# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import NSPrintOperation, NSApp, NSPortraitOrientation, NSFitPagination, NSOffState, NSOnState, NSControlTextDidChangeNotification, NSEventTrackingRunLoopMode, NSAlert, NSAlertFirstButtonReturn

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

import datetime
import objc
import os
import re
import hashlib
import uuid

from collections import OrderedDict
import pgpy
from pgpy.constants import PubKeyAlgorithm, KeyFlags, HashAlgorithm, SymmetricKeyAlgorithm, CompressionAlgorithm
import json
import socket
import time
import string
import random
import urllib
from http.client import RemoteDisconnected

from Crypto.Protocol.KDF import PBKDF2
from binascii import unhexlify, hexlify
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.queue import EventQueue
from application.system import makedirs
from zope.interface import implementer
from resources import ApplicationData

from sipsimple.configuration import DuplicateIDError
from sipsimple.addressbook import AddressbookManager, Group
from sipsimple.account import AccountManager, BonjourAccount, Account
from sipsimple.core import SIPURI, Message, FromHeader, ToHeader, RouteHeader, Route
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.payloads import ParserError
from sipsimple.payloads.iscomposing import IsComposingMessage, IsComposingDocument
from sipsimple.payloads.imdn import IMDNDocument, DeliveryNotification, DisplayNotification
from sipsimple.streams.msrp.chat import CPIMPayload, CPIMParserError, ChatIdentity
from sipsimple.threading import run_in_thread
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp

from twisted.internet import reactor

from ChatViewController import MSG_STATE_SENT, MSG_STATE_DELIVERED, MSG_STATE_DISPLAYED, MSG_STATE_FAILED

from BlinkLogger import BlinkLogger
from KeyEscrow import (escrow_is_missing, install_keypair, log_self_contact,
                       restore_from_own_contact, write_self_keys)
from HistoryManager import ChatHistory
from SMSViewController import SMSViewController, is_otr_wire_text
from MessageHost import peaks_metadata, reply_metadata


def _describe_payload_value(value):
    """A payload field, shortened only where it would be a wall of numbers.

    Long arrays and base64 blobs are exactly the fields worth confirming
    the PRESENCE of -- a recording's peaks are hundreds of numbers and its
    spectrum tens of kilobytes -- so they report their size and a sample
    instead of their contents.
    """
    if isinstance(value, (list, tuple)):
        if len(value) > 8:
            return '[%d values: %s ...]' % (len(value),
                                            ', '.join(str(v) for v in value[:8]))
        return repr(list(value))
    if isinstance(value, dict):
        return '{%s}' % ', '.join('%s: %s' % (k, _describe_payload_value(v))
                                  for k, v in sorted(value.items()))
    if isinstance(value, str) and len(value) > 120:
        return '<%d chars: %s...>' % (len(value), value[:60])
    return repr(value)
from MessageHost import (FILE_TRANSFER_CONTENT_TYPE, FILE_TRANSFER_CONTENT_TYPES,
                         file_transfer_envelope,
                         pgp_plaintext,
                         pgp_plaintext_bytes, public_key_short_checksum)
from SylkLocation import (LOCATION_CONTENT_TYPE, LEGACY_LOCATION_CONTENT_TYPE,
                          is_notable_action,
                          bubble_id as location_bubble_id, location_payload,
                          merge_location_bodies, storable_envelope)
from FileTransferCache import FILE_TRANSFER_PATH, base_url_from_transfer
from util import format_identity_to_string, run_in_gui_thread, call_later

unpad = lambda s: s[:-ord(s[len(s) - 1:])]


def generate_pgp_keypair(account):
    """Generate a fresh 4096-bit RSA PGP key pair for the account and persist
    it to the keys directory, updating the account settings. Any existing key
    files for this account are overwritten. This is the single place PGP keys
    are created; it must only be called after the user has explicitly opted in
    via the generate-key modal — never automatically."""
    keys_path = ApplicationData.get('keys')
    makedirs(keys_path)

    private_key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 4096)
    uid = pgpy.PGPUID.new(account.display_name, comment='Blink client', email=account.id)
    private_key.add_uid(uid, usage={KeyFlags.Sign, KeyFlags.EncryptCommunications, KeyFlags.EncryptStorage},
                             hashes=[HashAlgorithm.SHA512],
                             ciphers=[SymmetricKeyAlgorithm.AES256],
                             compression=[CompressionAlgorithm.Uncompressed])

    private_key_path = "%s/%s.privkey" % (keys_path, account.id)
    with open(private_key_path, "wb+") as fd:
        fd.write(str(private_key).encode())
    BlinkLogger().log_info("My PGP private key saved to %s" % private_key_path)

    public_key_path = "%s/%s.pubkey" % (keys_path, account.id)
    with open(public_key_path, "wb+") as fd:
        fd.write(str(private_key.pubkey).encode())
    BlinkLogger().log_info("My PGP public key saved to %s" % public_key_path)

    account.sms.private_key = private_key_path
    account.sms.public_key = public_key_path
    account.sms.public_key_checksum = hashlib.sha1(str(private_key.pubkey).encode()).hexdigest()
    account.save()
    return private_key_path


@implementer(IObserver)
class SMSWindowController(NSWindowController):

    tabView = objc.IBOutlet()
    tabSwitcher = objc.IBOutlet()
    toolbar = objc.IBOutlet()
    encryptionMenu = objc.IBOutlet()
    encryptionIconMenuItem = objc.IBOutlet()

    def initWithOwner_(self, owner):
        self = objc.super(SMSWindowController, self).init()
        if self:
            self._owner = owner
            NSBundle.loadNibNamed_owner_("SMSSession", self)
            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, name="BlinkShouldTerminate")
            self.notification_center.add_observer(self, name="ChatStreamOTREncryptionStateChanged")
            self.notification_center.add_observer(self, name="OTREncryptionDidStop")
            self.notification_center.add_observer(self, name="PGPEncryptionStateChanged")
            self.notification_center.add_observer(self, name="PGPPublicKeyReceived")

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

            if session.account is BonjourAccount():
                title = NSLocalizedString("Short Messages with %s", "Window Title") % display_name
                title = title + ' (Bonjour)'
            else:
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
    def _NH_PGPEncryptionStateChanged(self, sender, data):
        self.updateEncryptionWidgets()

    @objc.python_method
    def _NH_PGPPublicKeyReceived(self, sender, data):
        self.updateEncryptionWidgets()

    @objc.python_method
    def _NH_OTREncryptionDidStop(self, sender, data):
        self.updateEncryptionWidgets()

    def menuWillOpen_(self, menu):
        pass

    @objc.python_method
    def conversationBecameVisible(self, session):
        """The user is now looking at this conversation.

        Clears the contact-row unread badge as well as the tab badge: since
        an arriving message no longer creates a conversation, the contact
        badge is the only unread signal the user gets, and it has to go away
        the moment they actually read the messages.
        """
        if session is None:
            return
        try:
            if SMSWindowManager().clearUnreadMessages(session.remote_uri):
                session.announce_conversation_read()
        except Exception as e:
            BlinkLogger().log_error('Cannot clear unread for %s: %s' % (session, e))

    def noteNewMessageForSession_(self, session):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(session)

        if index == NSNotFound:
            return

        tabItem = self.tabView.tabViewItemAtIndex_(index)
        item = self.tabSwitcher.itemForTabViewItem_(tabItem)

        if not item:
            return

        count = self.unreadMessageCounts.get(session, 0)
        count = self.unreadMessageCounts[session] = count + 1

        if self.tabView.selectedTabViewItem() == tabItem:
            session = self.selectedSessionController()
            if self.window().isKeyWindow():
                item.setBadgeLabel_("")
                del self.unreadMessageCounts[session]
                session.not_read_queue_start()
                self.conversationBecameVisible(session)
            else:
                item.setBadgeLabel_(str(count))
        else:
            item.setBadgeLabel_(str(count))
            session.not_read_queue_stop()

    def noteNoMessageForSession_(self, session):
        print('noteNoMessageForSession_')
        index = self.tabView.indexOfTabViewItemWithIdentifier_(session)

        if index == NSNotFound:
            return

        tabItem = self.tabView.tabViewItemAtIndex_(index)
        item = self.tabSwitcher.itemForTabViewItem_(tabItem)

        if not item:
            return

        item.setBadgeLabel_("")
        try:
            del self.unreadMessageCounts[session]
        except KeyError:
            pass
        self.conversationBecameVisible(session)

    def noteView_isComposing_(self, smsview, flag):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(smsview)
        if index == NSNotFound:
            return
        tabItem = self.tabView.tabViewItemAtIndex_(index)
        item = self.tabSwitcher.itemForTabViewItem_(tabItem)
        if item:
            item.setComposing_(flag)

    @objc.python_method
    def addViewer(self, viewer, focusTab=False):
        tabItem = NSTabViewItem.alloc().initWithIdentifier_(viewer)
        tabItem.setView_(viewer.getContentView())
        sip_address = '%s@%s' % (viewer.target_uri.user.decode(), viewer.target_uri.host.decode())
        if viewer.display_name and viewer.display_name != sip_address:
            tabItem.setLabel_("%s" % viewer.display_name)
        else:
            tabItem.setLabel_(format_identity_to_string(viewer.target_uri))
        self.tabSwitcher.addTabViewItem_(tabItem)
        if len(list(self.viewers)) == 1 or focusTab:
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
        session = self.selectedSessionController()

        self.updateEncryptionWidgets(session)

        for viewer in self.viewers:
            if viewer != session:
                viewer.not_read_queue_stop()
            else:
                if self.window().isKeyWindow():
                    _item = self.tabSwitcher.itemForTabViewItem_(item)
                    _item.setBadgeLabel_("")
                    # The tabbed window announces the read the same way the
                    # pane does; it used to call a stub that returned
                    # immediately, so selecting a tab told nobody.
                    viewer.announce_conversation_read()
                    viewer.not_read_queue_start()
                    self.conversationBecameVisible(viewer)

        try:
            del self.unreadMessageCounts[item.identifier()]
        except KeyError:
            pass
        else:
            self.noteNewMessageForSession_(item.identifier())

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

    def windowDidResignKey_(self, notification):
        session = self.selectedSessionController()
        if session:
            session.not_read_queue_stop()

    def windowDidBecomeKey_(self, notification):
        session = self.selectedSessionController()
        if session:
            session.not_read_queue_start()
            self.conversationBecameVisible(session)
    
        tabItem = self.tabView.selectedTabViewItem()

        if tabItem.identifier() in self.unreadMessageCounts:
            del self.unreadMessageCounts[tabItem.identifier()]

        item = self.tabSwitcher.itemForTabViewItem_(tabItem)
        item.setBadgeLabel_("")

    @objc.IBAction
    def requestPublicKey_(self, sender):
        session = self.selectedSessionController()
        if session:
            session.requestPublicKey()

    @objc.IBAction
    def sendMyPublicKey_(self, sender):
        session = self.selectedSessionController()
        if session:
            session.sendMyPublicKey(force=True)

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

            item = menu.itemWithTag_(4)
            item.setHidden_(True)

            item = menu.itemWithTag_(5)
            item.setHidden_(True)

            item = menu.itemWithTag_(6)
            item.setHidden_(True)

            item = menu.itemWithTag_(8)
            item.setHidden_(True)

            item = menu.itemWithTag_(9)
            item.setEnabled_(False)
            item.setHidden_(True)

            item = menu.itemWithTag_(10)
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

                item = menu.itemWithTag_(11)
                item.setHidden_('@' not in selectedSession.remote_uri or selectedSession.account is BonjourAccount())
#                item.setRepresentedObject_({'account': selectedSession.account, 'recipient': selectedSession.remote_uri})

                item = menu.itemWithTag_(2)
                item.setHidden_(False)
                if chat_stream.active:
                    item.setTitle_(NSLocalizedString("My fingerprint is %s", "Menu item") % str(chat_stream.key_fingerprint))

                else:
                    item.setEnabled_(False)
                    item.setTitle_(NSLocalizedString("OTR encryption is disabled in Chat preferences", "Menu item"))

                if settings.chat.enable_encryption:
                    if chat_stream.peer_fingerprint:

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

                if selectedSession.pgp_encrypted:
                    item = menu.itemWithTag_(8)
                    item.setHidden_(False)

                    item = menu.itemWithTag_(9)
                    item.setEnabled_(True)
                    item.setHidden_(False)

                    item = menu.itemWithTag_(10)
                    item.setHidden_(False)

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
            elif selectedSession.pgp_encrypted:
                self.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-green"))
            else:
                self.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("unlocked-darkgray"))
        elif selectedSession and selectedSession.pgp_encrypted:
            self.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-green"))
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

        # the message list itself, not the scroll view around it, so the whole
        # conversation paginates rather than just the visible page
        NSPrintOperation.printOperationWithView_(
            self.selectedSessionController().chatViewController.messageListView).runOperation()

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
    # viewer -> host. A host is anything implementing the protocol in
    # MessageHost.py: an SMSWindowController today, a MessagePaneController
    # once the drawer lands. Treated as a cache; windowForViewer falls back
    # to scanning self.windows and repopulates on a miss.
    viewer_hosts = {}
    heartbeat_timer = None
    # True while a cached journal is being applied in bulk. Journal apply must
    # not spin up conversation viewers: each one loads a nib, re-imports the
    # PGP keys from disc, queries history and renders a page of bubbles on the
    # GUI thread -- 14 of them during one sync is what made the app unusable.
    # Messages still reach any viewer the user already has open.
    _journal_bulk = False
    # canonical remote uri -> unread count. The only place unread lives now
    # that an arriving message no longer creates a conversation.
    unread_counts = {}
    # canonical remote uri -> naive UTC datetime of the newest message we
    # know about. Seeded once from history, kept current by every path that
    # stores a message. The Messages group is ordered by this.
    last_message_times = {}
    _order_changed_during_bulk = False
    _unread_changed_during_bulk = set()
    # Resolved SIP routes, shared by every conversation. Keyed by the actual
    # inputs of the lookup, so an account using an outbound proxy (or talking
    # to its own domain) collapses to ONE entry for all its contacts, and
    # other domains collapse to one entry per domain.
    #
    # Deliberately never expires on time: a route stays until a send over it
    # fails. Settings changes need no invalidation either -- tls_name, the
    # proxy and the transport list are all part of the key, so changing one
    # simply produces a different entry.
    route_cache = {}
    # Ids of the messages already taken in, oldest first. A message can
    # arrive twice for two unrelated reasons -- the transport retransmitting
    # the request when its answer went missing, and the sender giving up on
    # that and sending the message again under a new transaction -- and the
    # two look nothing alike on the wire: the first repeats the Call-Id, the
    # second repeats only the CPIM Message-ID. Both are asked of this ring.
    #
    # Bounded, because a client left running for weeks would otherwise
    # remember every message it has ever been handed. Ordered, so what is
    # dropped when it is full is the oldest id rather than an arbitrary one.
    seen_message_ids = OrderedDict()
    MAX_SEEN_MESSAGE_IDS = 10000
    import_key_window = None
    export_key_window = None
    syncConversationsInProgress = {}
    pendingSaveMessage = {}
    # the last figure the progress line printed, so an unchanged gauge
    # stays quiet instead of reprinting itself on every unrelated save
    _pending_save_logged = None
    new_contacts = set()
    private_keys = {}
    # Accounts whose addressbook has answered the escrow question, those
    # that have adopted an escrowed key, and the escrow each one last
    # failed to open -- see restorePrivateKeyFromOwnContact.
    key_escrow_checked = set()
    key_restore_done = set()
    key_restore_failed = {}
    # Last reason a journal sync was skipped, per account, so a standing
    # reason is stated once instead of on every registration refresh.
    sync_skip_logged = {}
    # Last file transfer URL derived per account, so the guess is stated
    # when it changes rather than once per transfer.
    transfer_url_logged = {}
    # Accounts whose missing escrow has been repaired this session, so a
    # write that keeps failing is attempted once rather than per reload.
    escrow_repaired = set()
    generate_prompt_deferred = {}

    def init(self):
        self = objc.super(SMSWindowManagerClass, self).init()
        if self:
            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, name="SIPEngineGotMessage")
            self.notification_center.add_observer(self, name="SIPAccountDidActivate")
            self.notification_center.add_observer(self, name="CFGSettingsObjectDidChange")
            self.notification_center.add_observer(self, name="SIPAccountRegistrationDidSucceed")
            self.notification_center.add_observer(self, name="MessageSaved")
            self.notification_center.add_observer(self, name="XCAPManagerDidReloadData")
            self.keys_path = ApplicationData.get('keys')
            makedirs(self.keys_path)
            self.history = ChatHistory()
            self.contacts_queue = EventQueue(self.handle_contacts_queue)
            self.contacts_queue.start()

            # The heartbeat lives on the manager rather than on a window so
            # that every live conversation keeps retrying failed messages and
            # re-resolving routes, whether or not a window is hosting it.
            self.heartbeat_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(10.0, self, "heartbeatTimer:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.heartbeat_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.heartbeat_timer, NSEventTrackingRunLoopMode)

            from MessageHost import describe_configuration
            BlinkLogger().log_info("Message UI model: %s" % describe_configuration())

            # Order the Messages group by recency from the first draw,
            # rather than alphabetically until the first message arrives,
            # and put back the badges for whatever was left unread.
            self.loadLastMessageTimes()
            self.loadUnreadCounts()

        return self

    def heartbeatTimer_(self, timer):
        # Guarded per viewer. One conversation raising used to end the whole
        # tick: every viewer after it in the iteration lost its heartbeat,
        # and with it the only thing that resends a message stranded by a
        # failed route.
        for viewer in list(self.allViewers()):
            try:
                viewer.heartbeat()
            except Exception as e:
                BlinkLogger().log_error('Heartbeat failed for %s: %s' % (viewer.remote_uri, e))

    @objc.python_method
    def allViewers(self):
        """Every live viewer, whatever is hosting it.

        Also prunes viewer_hosts: a closed window is removed from self.windows
        (windowShouldClose_) and drops its tab items, so its viewers must not
        keep receiving heartbeats.
        """
        seen = set()
        for window in list(self.windows):
            for viewer in window.viewers:
                if viewer not in seen:
                    seen.add(viewer)
                    yield viewer
        for viewer, host in list(self.viewer_hosts.items()):
            if viewer in seen:
                continue
            if host is None or not self._hostHasViewer(host, viewer):
                self.viewer_hosts.pop(viewer, None)
                continue
            seen.add(viewer)
            yield viewer

    @objc.python_method
    def _NH_XCAPManagerDidReloadData(self, sender, data):
        # Step 1 of the cross-client key escrow work: read-only. Every time
        # the addressbook comes back from the server, log what our own
        # contact carries, so the escrow's actual location is settled from
        # inside a running Blink rather than inferred from a dumped document.
        # Nothing here writes, and nothing downstream depends on it yet.
        try:
            account = sender.account
        except (AttributeError, ReferenceError):
            return
        # The addressbook has now answered for this account, so a generate
        # prompt held back waiting for it can go ahead.
        self.key_escrow_checked.add(account.id)

        # Restore BEFORE reporting. The report describes the state of the
        # account, and a restore in the same pass changes it: reporting first
        # printed "no private key configured" and "a write would be REFUSED --
        # nothing to escrow" three lines above the line saying the key had just
        # been installed. Restoring first makes one pass tell one story.
        try:
            self.restorePrivateKeyFromOwnContact(account)
        except Exception as e:
            BlinkLogger().log_error('Key escrow restore failed for %s: %s'
                                    % (getattr(account, 'id', '?'), e))

        try:
            log_self_contact(account)
        except Exception as e:
            BlinkLogger().log_error('Key escrow inspection failed for %s: %s'
                                    % (getattr(account, 'id', '?'), e))

        try:
            self.repairMissingEscrow(account)
        except Exception as e:
            BlinkLogger().log_error('Key escrow repair failed for %s: %s'
                                    % (getattr(account, 'id', '?'), e))

    @objc.python_method
    def repairMissingEscrow(self, account):
        """Put the escrow back when the server has lost it.

        An escrow can be destroyed by something entirely outside this feature:
        a client that adds a contact the addressbook already holds replaces the
        element and drops the attributes it does not know about. Observed on
        2026-08-28, when a fresh profile with two XCAP accounts wiped the key
        escrow off the second account's contacts; the key came back only
        because a phone noticed and re-escrowed it.

        So an account holding a key with nothing escrowed anywhere repairs
        itself. This is deliberately the ONLY automatic write: no escrow means
        writing one cannot displace anyone's key, while every other mismatch
        between a local key and an escrow is a decision for the user. The
        blockers still apply, so a contact shared with another account is
        refused here as everywhere else.
        """
        if not escrow_is_missing(account):
            self.escrow_repaired.discard(account.id)
            return
        if account.id in self.escrow_repaired:
            return

        self.escrow_repaired.add(account.id)
        BlinkLogger().log_info('Key escrow: %s holds a key but the server carries no escrow for it '
                               '-- restoring the backup' % account.id)
        written, reason = write_self_keys(account)
        if not written:
            BlinkLogger().log_info('Key escrow: could not restore the backup for %s -- %s'
                                   % (account.id, reason))

    @objc.python_method
    def restorePrivateKeyFromOwnContact(self, account):
        """Adopt an escrowed keypair when this device has none.

        Runs on every addressbook reload, which is several times per sign-in,
        so both outcomes are latched: success needs no repeat, and a failure
        keyed on the blob it failed against must not produce an identical
        complaint per reload. The key includes the password length so that
        correcting the password retries at once.
        """
        if account.id in self.key_restore_done:
            return
        if account.sms.private_key and os.path.exists(account.sms.private_key):
            return

        from KeyEscrow import read_self_keys
        record = read_self_keys(account)
        if record is None:
            return

        password = (account.auth.password or '').strip()
        signature = '%s#%s#%d#%d' % (account.id, record.get('timestamp', '?'),
                                     len(record.get('private_key') or ''), len(password))
        if self.key_restore_failed.get(account.id) == signature:
            return

        restored, reason = restore_from_own_contact(account)
        if restored:
            self.key_restore_done.add(account.id)
            self.private_keys.pop(account.id, None)   # drop the cached miss
            nc_title = NSLocalizedString("Private key", "System notification title")
            nc_body = NSLocalizedString("The private key of this account was restored from the server", "System notification body")
            NSApp.delegate().gui_notify(nc_title, nc_body, str(account.id))
        elif reason:
            self.key_restore_failed[account.id] = signature
            BlinkLogger().log_info('Key escrow: not restoring for %s -- %s' % (account.id, reason))

    @objc.python_method
    def _NH_CFGSettingsObjectDidChange(self, account, data):
        if isinstance(account, Account):
            if 'sms.history_token' in data.modified:
                if account.sms.history_token:
                    BlinkLogger().log_info("Sync token for account %s has been updated" % account.id)
                    self.syncConversations(account)
                else:
                    BlinkLogger().log_info("Sync token for account %s has been removed" % account.id)
                    account.sms.history_last_id = None
                    account.sms.enable_replication = False
                    account.save()

            if 'sms.history_url' in data.modified:
                if account.sms.history_url:
                    BlinkLogger().log_info("Sync url for account %s has been updated: %s" % (account.id, account.sms.history_url))
                else:
                    account.sms.history_last_id = None
                    account.sms.history_token = None
                    account.sms.enable_replication = False
                    account.save()

            if 'sms.enable_replication' in data.modified:
                if account.sms.enable_replication:
                    self.requestSyncToken(account)

    @objc.python_method
    def _NH_SIPAccountDidActivate(self, account, data):
       pass
       #BlinkLogger().log_info("Account %s activated" % account.id)

    @objc.python_method
    def _resolvePendingSave(self, msgid):
        """Take a message off the pending gauge when no save will happen.

        Every entry put on the gauge is waiting for a MessageSaved to take
        it off again. The journal path has two ways out that never reach
        add_message -- a trail tick that folds into an existing row, and a
        payload we cannot decode -- and each of those used to leave its
        entry behind for good.
        """
        return self.pendingSaveMessage.pop(msgid, None) is not None

    @objc.python_method
    def _NH_MessageSaved(self, sender, data):
        """Report progress when the gauge moves, and only then.

        Every add_message in the application posts this, including the
        thousands a bulk journal apply writes, and those were never on the
        gauge. The old code let them all fall through to the modulo test
        against an unchanged number: with a few stale entries stuck on the
        gauge at a multiple of ten, every unrelated save reprinted the same
        line -- forty pending, forty pending, forty pending -- while the
        journal was busy doing something else entirely.
        """
        if not self.pendingSaveMessage.pop(data.msgid, None):
            return                      # not ours to count; nothing has moved

        remaining_messages = len(self.pendingSaveMessage)
        if remaining_messages == 0:
            self._pending_save_logged = None
            #BlinkLogger().log_info('Sync conversations completed')
            return

        if remaining_messages > 1000:
            step = 1000
        elif remaining_messages > 100:
            step = 100
        else:
            step = 10

        if remaining_messages % step:
            return
        if remaining_messages == self._pending_save_logged:
            return

        self._pending_save_logged = remaining_messages
        BlinkLogger().log_info('%d pending history messages' % remaining_messages)

    @objc.python_method
    def _NH_SIPAccountRegistrationDidSucceed(self, account, data):
        # BlinkLogger().log_info('startup: SMSWindowManager._NH_SIPAccountRegistrationDidSucceed enter (%s)' % account.id)
        if account is not BonjourAccount():
            call_later(10, self.syncConversations, account)
        # BlinkLogger().log_info('startup: SMSWindowManager._NH_SIPAccountRegistrationDidSucceed exit')


    @objc.python_method
    def requestSyncToken(self, account):
        if not account.sms.enable_replication:
            BlinkLogger().log_info('Sync conversations is disabled for account %s' % account.id)
            return

        self.requestApiToken(account, 'history sync')

    @objc.python_method
    def requestApiToken(self, account, reason=''):
        """Ask the server for this account's API token.

        Deliberately not gated on enable_replication, and deliberately
        not the same thing as requestSyncToken: the token is no longer
        only the journal's credential. File uploads present it too, and
        an account that has replication switched off still has to be able
        to send a file.

        At most one request per account per TOKEN_REQUEST_INTERVAL. Two
        different failures now ask for a refresh -- a rejected journal
        page and a rejected upload -- and several uploads can be refused
        in the same second; without this each one would send its own
        request and each answer would restart a journal sync.
        """
        last = self._token_requested_at.get(account.id, 0)
        now = time.time()
        if now - last < self.TOKEN_REQUEST_INTERVAL:
            BlinkLogger().log_debug('An API token for %s was requested %.0fs ago; not asking again'
                                    % (account.id, now - last))
            return False
        self._token_requested_at[account.id] = now
        BlinkLogger().log_info('Requesting an API token for %s%s'
                               % (account.id, (' (%s)' % reason) if reason else ''))
        self.sendMessage(account, 'I need a token', 'application/sylk-api-token')
        return True

    @objc.python_method
    @run_in_green_thread
    def sendMessage(self, account, content, content_type, recipient=None):
        # tls_name must be carried into the lookup: without it the route is
        # verified against the DNS-resolved name rather than the account's
        # configured TLS name, so an account whose proxy answers under a
        # different identity fails certificate verification and the request
        # is never delivered. Every other lookup_sip_proxy call site in the
        # app passes it; this one did not, which is why token requests over
        # TLS silently never arrived and history sync stayed stuck on 401.
        tls_name = account.sip.tls_name or account.id.domain
        if account.sip.outbound_proxy is not None:
            proxy = account.sip.outbound_proxy
            uri = SIPURI(host=proxy.host, port=proxy.port, parameters={'transport': proxy.transport})
            tls_name = account.sip.tls_name or proxy.host
            BlinkLogger().log_info("Starting DNS lookup via proxy %s" % uri)
        elif account.sip.always_use_my_proxy:
            uri = SIPURI(host=account.id.domain)
            tls_name = account.sip.tls_name or account.id.domain
            BlinkLogger().log_info("Starting DNS lookup via proxy of account %s" % account.id)
        else:
            uri = SIPURI.parse('sip:%s' % account.id)

        settings = SIPSimpleSettings()
        lookup = DNSLookup()

        BlinkLogger().log_info('Token request for %s: uri=%s account.sip.tls_name=%r using tls_name=%r'
                              % (account.id, uri, account.sip.tls_name, tls_name))

        try:
           routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list, tls_name=tls_name).wait()
        except DNSLookupError as e:
           BlinkLogger().log_info('DNS Lookup error for token request: %s' % str(e))
        else:
            if not routes:
               BlinkLogger().log_info('DNS Lookup failed for token request, no routes found')
               return

            route = routes[0]
            BlinkLogger().log_info('Sending %s message to %s' % (content_type, route.uri))
            from_uri = SIPURI.parse('sip:%s' % account.id)
            if recipient:
                to_uri = SIPURI.parse('sip:%s' % recipient)
            else:
                to_uri = SIPURI.parse('sip:%s' % account.id)

            message_request = Message(FromHeader(from_uri), ToHeader(to_uri), RouteHeader(route.uri), content_type, content.encode(), credentials=account.credentials)

            message_request.send()

    @objc.python_method
    @run_in_thread('contact_sync')
    def handle_contacts_queue(self, payload):
        content = payload['data']
        account = payload['account']
        if content.startswith('-----BEGIN PGP MESSAGE-----') and content.endswith('-----END PGP MESSAGE-----'):
            try:
                private_key = self.private_keys[account]
            except KeyError:
                private_key_path = "%s/%s.privkey" % (self.keys_path, account)
            
                try:
                    private_key, _ = pgpy.PGPKey.from_file(private_key_path)
                except Exception as e:
                    BlinkLogger().log_error('Cannot import PGP private key from %s: %s' % (private_key_path, str(e)))
                    return
                else:
                    BlinkLogger().log_info('PGP private key imported from %s' % private_key_path)
                    self.private_keys[account] = private_key

            if private_key:
                try:
                    pgpMessage = pgpy.PGPMessage.from_blob(content.strip())
                    decrypted_message = private_key.decrypt(pgpMessage)
                except (pgpy.errors.PGPDecryptionError, pgpy.errors.PGPError) as e:
                    BlinkLogger().log_info('PGP decryption failed for contact update')
                    return
                else:
                    content = pgp_plaintext(decrypted_message) or ''

        try:
            contact_data = json.loads(content)
            uri = contact_data['uri']
            try:
                display_name = contact_data['name']
            except KeyError:
                display_name = uri
            organization = contact_data['organization']
            self.saveContact(uri, {'name': display_name or uri, 'organization': organization})
        except (TypeError, KeyError, json.decoder.JSONDecodeError) as e:
            BlinkLogger().log_error('Failed to update contact %s: %s' % (content, str(e)))

    @objc.python_method
    @run_in_thread('sms_sync')
    def request_token(self, account):
        """A journal page was refused. Drop the sync guard and ask again.

        The guard is released here because the sync that held it has just
        given up half way; nothing else will release it, and until it is
        gone the retry cannot start.
        """
        BlinkLogger().log_info('Request another token...')
        try:
           del self.syncConversationsInProgress[account.id]
        except KeyError:
           pass
        self.requestSyncToken(account)

    @objc.python_method
    def requestUploadToken(self, account):
        """An upload was refused with 401. Ask for a token, and only that.

        Nothing to do with journal sync: this must not touch its guard,
        because a sync may well be running normally at the time, and
        deleting the guard under it lets a second one start alongside.
        """
        self.requestApiToken(account, 'a rejected upload')
                   
    # Per account, when an API token was last asked for. Class level like
    # everything else here -- there is one window manager.
    _token_requested_at = {}
    TOKEN_REQUEST_INTERVAL = 30.0

    MAX_JOURNAL_PAGES = 200

    # How far back a first sync reaches, in years, when there is no cursor.
    #
    # The storage layer applies `now() - 3 days` when a request carries
    # neither a message id nor a `since` (storage.py, CassandraMessageStorage
    # .__getitem__). Measured against an account with ~10000 entries: 1006
    # came back, and the next request -- made from the newest id on that page,
    # since a path cursor only moves forward -- correctly returned nothing.
    # The older entries were never unreachable, they were never asked for.
    #
    # Five years rather than the epoch, matching what sylk mobile asks for in
    # requestSyncConversations: wide enough for any real account history, and
    # a bounded date the server can answer from an index rather than a
    # sentinel that asks it to consider every row it has ever stored.
    #
    # This needs the matching SylkServer fix: /messages/history did not read
    # `since` from the query string at all, so the storage layer's third key
    # element was absent and defaulted to None. Against an unpatched server
    # the parameter is ignored and the three-day window still applies.
    JOURNAL_SINCE_YEARS = 5
    # how often to report progress while grinding through a cached page
    JOURNAL_PROGRESS_EVERY = 250
    # seconds to pause every JOURNAL_PROGRESS_EVERY entries, so the GUI and
    # the DB queue get air while a large page is applied
    JOURNAL_THROTTLE = 0.05
    # fewer senders than this in one offline batch -> a named banner each;
    # this many or more -> a single "from N contacts" banner
    NAMED_OFFLINE_NOTIFICATION_LIMIT = 4

    @objc.python_method
    def journalDirectory(self, account):
        path = ApplicationData.get('journal/%s' % account.id)
        makedirs(path)
        return path

    @objc.python_method
    def journalSinceWindow(self):
        """The `since` a cursor-less first sync asks from, as an ISO timestamp.

        Shaped like the timestamps the websocket clients send (a JavaScript
        Date through JSON), because that is the format the storage layer has
        always been fed.
        """
        when = datetime.datetime.utcnow() - datetime.timedelta(days=365 * self.JOURNAL_SINCE_YEARS)
        return when.isoformat(timespec='milliseconds') + 'Z'

    @objc.python_method
    def _journalFileName(self, messages):
        """Sortable file name: the page's last timestamp, then its last id.

        Cached pages are applied in sorted order, so the name has to sort
        chronologically. ISO timestamps do, once punctuation is flattened.
        """
        last = messages[-1]
        stamp = re.sub(r'[^0-9A-Za-z]', '-', str(last.get('timestamp') or '')) or 'unknown'
        return '%s-%s.json' % (stamp, last.get('message_id') or 'page')

    @objc.python_method
    def _fetchJournalPage(self, account, url):
        """GET one journal page. Returns (messages, status): ok / auth / error."""
        req = urllib.request.Request(url, method="GET")
        req.add_header('Authorization', 'Apikey %s' % account.sms.history_token)
        try:
            raw_response = urllib.request.urlopen(req, timeout=20)
        except (urllib.error.URLError, ConnectionRefusedError, TimeoutError,
                socket.timeout, RemoteDisconnected) as e:
            BlinkLogger().log_info('SylkServer connection error for %s: %s' % (url, str(e)))
            if getattr(e, 'code', None) == 401:
                return None, 'auth'
            return None, 'error'

        try:
            raw_data = raw_response.read().decode().replace('\\/', '/')
            json_data = json.loads(raw_data)
        except Exception as e:
            BlinkLogger().log_error('Error reading SylkServer response from %s: %s' % (url, str(e)))
            return None, 'error'

        messages = json_data.get('messages') or []
        BlinkLogger().log_info('Fetched %d journal entries for %s from %s (%d bytes)'
                               % (len(messages), account.id, url, len(raw_data)))
        return messages, 'ok'

    @objc.python_method
    def _downloadJournal(self, account):
        """Stage 1: page through the server journal, caching each page on disc.

        Nothing is processed here -- no DB writes, no GUI work, no per-entry
        logging -- so the download runs at network speed. The stored cursor
        advances only once a page is safely written, so an interruption costs
        at most the page in flight rather than the whole journal.
        """
        directory = self.journalDirectory(account)
        base_url = account.sms.history_url.replace("@", "%40")
        pages = 0
        entries = 0

        while pages < self.MAX_JOURNAL_PAGES:
            cursor = account.sms.history_last_id
            if cursor:
                url = "%s/%s" % (base_url, cursor)
            else:
                url = "%s?since=%s" % (base_url, self.journalSinceWindow())
            BlinkLogger().log_debug('Sync conversations from %s' % url)

            messages, status = self._fetchJournalPage(account, url)

            if status == 'auth':
                BlinkLogger().log_info('History token rejected for %s (401), requesting a new one in 30s' % account.id)
                reactor.callLater(30, self.request_token, account)
                return pages, entries
            if status != 'ok':
                BlinkLogger().log_info('Journal download for %s stopped after %d page(s): the last '
                                       'request to %s failed' % (account.id, pages, url))
                return pages, entries
            if not messages:
                break

            name = self._journalFileName(messages)
            path = os.path.join(directory, name)
            try:
                with open(path, 'w') as f:
                    # Store the cursor as it stood BEFORE this page: the apply
                    # stage needs it to tell a first-ever backfill (persist
                    # only) from a catch-up (present in the GUI), and by then
                    # the account cursor has advanced past every cached page.
                    json.dump({'cursor': cursor or '', 'messages': messages}, f)
            except Exception as e:
                BlinkLogger().log_error('Cannot write journal file %s: %s' % (path, e))
                return pages, entries

            pages += 1
            entries += len(messages)
            BlinkLogger().log_info('Cached journal page %s (%d entries)' % (name, len(messages)))

            last_message_id = messages[-1].get('message_id')
            if last_message_id:
                account.sms.history_last_id = last_message_id
                account.save()
        else:
            BlinkLogger().log_error('Journal download for %s hit the %d page cap; more may remain'
                                    % (account.id, self.MAX_JOURNAL_PAGES))

        return pages, entries

    @objc.python_method
    def _applyCachedJournals(self, account):
        """Stage 2: apply cached pages oldest to newest, one file at a time.

        Each file is deleted once applied, so an interrupted run resumes with
        whatever is left instead of starting over.
        """
        directory = self.journalDirectory(account)
        try:
            names = sorted(n for n in os.listdir(directory) if n.endswith('.json'))
        except OSError as e:
            BlinkLogger().log_error('Cannot list journal directory %s: %s' % (directory, e))
            return

        if not names:
            return

        BlinkLogger().log_info('Applying %d cached journal pages for %s' % (len(names), account.id))
        all_contacts = set()
        all_incoming = {}

        # One pause for the whole run: EventQueue.pause/unpause is not a
        # counter, so pausing per page and unpausing once would be unbalanced.
        self.contacts_queue.pause()
        self._journal_bulk = True
        try:
            self._applyJournalFiles(account, directory, names, all_contacts, all_incoming)
        finally:
            self._journal_bulk = False
            self.contacts_queue.unpause()

        self._finishJournalApply(account, all_contacts, all_incoming)

    @objc.python_method
    def _applyJournalFiles(self, account, directory, names, all_contacts, all_incoming):
        for index, name in enumerate(names, 1):
            path = os.path.join(directory, name)
            try:
                with open(path) as f:
                    payload = json.load(f)
                messages = payload.get('messages') or []
                cursor = payload.get('cursor') or None
                BlinkLogger().log_info('Applying journal %d/%d: %s (%d entries)'
                                       % (index, len(names), name, len(messages)))
                _, contacts, incoming, summary, unhandled = self._applyJournalEntries(account, messages, cursor, label=name)
                all_contacts |= contacts
                for contact, count in incoming.items():
                    all_incoming[contact] = all_incoming.get(contact, 0) + count
                self._logJournalSummary(account, summary, unhandled=unhandled)
            except Exception as e:
                BlinkLogger().log_error('Error applying journal %s: %s' % (name, e))
                import traceback
                traceback.print_exc()
            try:
                os.unlink(path)
            except OSError as e:
                BlinkLogger().log_error('Cannot delete applied journal %s: %s' % (path, e))

    @objc.python_method
    def _applyJournalEntries(self, account, messages, cursor, label=''):
        """Dispatch one cached journal page. No network, no file I/O.

        `cursor` is the sync cursor as it stood before this page was
        downloaded; syncIncoming/OutgoingMessage use it to tell a first-ever
        backfill from a catch-up.
        """
        sync_contacts = set()
        sync_incoming = {}
        sync_summary = {}
        sync_unhandled = {}
        last_message_id = None

        total = len(messages)
        started = datetime.datetime.now()
        # The rate is measured over the LAST chunk and with the throttle
        # taken out of it. Reporting i/elapsed instead had two faults that
        # compounded: it was a cumulative average, so the first sample was
        # a meaningless 75000/s and every later one was dragged towards it
        # rather than saying how fast the apply was going NOW; and elapsed
        # included the deliberate sleep between chunks, which at 0.05s per
        # 250 entries is most of the wall clock on a page this quick -- so
        # the figure was mostly measuring the pause, not the work.
        slept = 0.0
        window_start = started
        window_from = 0
        i = 0
        for msg in messages:
            #BlinkLogger().log_info('Process journal %d: %s' % (i, msg['timestamp']))
            i = i + 1

            if total and (i % self.JOURNAL_PROGRESS_EVERY == 0 or i == total):
                now = datetime.datetime.now()
                window = (now - window_start).total_seconds()
                working = max((now - started).total_seconds() - slept, 0.0)
                rate = ((i - window_from) / window) if window > 0.001 else None
                overall = (i / working) if working > 0.001 else None
                if overall and i < total:
                    left = '  %.0fs left' % ((total - i) / overall)
                else:
                    left = ''
                BlinkLogger().log_info(
                    'Journal %s: %d/%d (%d%%) %s entries/s, %.1fs working%s'
                    % (label or 'page', i, total, (100 * i) // total,
                       ('%.0f' % rate) if rate else '-', working, left))
                time.sleep(self.JOURNAL_THROTTLE)
                # Measured after the sleep, so the next window contains
                # none of it -- and so an overrun by the scheduler is
                # excluded exactly rather than assumed to be 0.05s.
                after = datetime.datetime.now()
                slept += (after - now).total_seconds()
                window_start = after
                window_from = i
            try:
                content_type = msg['content_type']
                last_message_id = msg['message_id']

                summary_contact = msg.get('contact') or '(no contact)'
                per_contact = sync_summary.setdefault(summary_contact, {})
                per_contact[content_type] = per_contact.get(content_type, 0) + 1

                if content_type == 'application/sylk-conversation-remove':
                    BlinkLogger().log_debug('Remove conversation with %s' % msg['content'])
                    self.history.delete_messages(local_uri=str(account.id), remote_uri=msg['content'])
                    self.history.delete_messages(local_uri=msg['content'], remote_uri=str(account.id))
                elif content_type == 'application/sylk-message-remove':
                    BlinkLogger().log_debug('Remove message %s with %s' % (msg['message_id'], msg['contact']))
                    self.history.delete_message(msg['message_id']);
                elif content_type == 'message/imdn':
                    payload = eval(msg['content'])
                    imdn_status = payload['state']
                    imdn_message_id = payload['message_id']
                    status = None
                    if imdn_status == 'delivered':
                        status = MSG_STATE_DELIVERED
                    elif imdn_status == 'displayed':
                        status = MSG_STATE_DISPLAYED
                    elif imdn_status == 'failed':
                        status = MSG_STATE_FAILED
                        
                    if status:
                        #BlinkLogger().log_info('Sync IMDN state %s for message %s' % (status, imdn_message_id))
                        self.pendingSaveMessage[imdn_message_id] = True
                        self.history.update_message_status(imdn_message_id, status)
                elif content_type == 'application/sylk-contact-update':
                    self.contacts_queue.put({'account': str(account.id), 'data': msg['content']})
                elif content_type == 'application/sylk-conversation-read':
                    # Replayed in order with the messages themselves, so a
                    # conversation read elsewhere after its last message ends
                    # up with a cleared badge, not a stale count.
                    self.applyConversationRead(account, msg['content'])
                elif content_type == 'text/pgp-public-key':
                    uri = msg['contact']
                    if msg.get('direction') == 'outgoing':
                        # Our own key, sent from some device to this contact.
                        # msg['contact'] is the recipient, so importing would
                        # overwrite their key with ours -- see the live path.
                        BlinkLogger().log_debug(
                            u'Skipping our own public key sent to %s' % uri)
                        continue
                    BlinkLogger().log_info(u"Public key from %s received" % (uri))
                    content = (msg['content'] or '').encode()

                    if AccountManager().has_account(uri):
                        BlinkLogger().log_debug(u"Public key save skipped for own accounts")
                        continue

                    public_key = ''
                    start_public = False

                    for l in content.decode().split("\n"):
                        if l == "-----BEGIN PGP PUBLIC KEY BLOCK-----":
                            start_public = True

                        if l == "-----END PGP PUBLIC KEY BLOCK-----":
                            public_key = public_key + l + '\n'
                            start_public = False
                            break

                        if start_public:
                            public_key = public_key + l + '\n'
                    
                    if public_key:
                        self._warn_if_key_mismatched(public_key, uri)
                        public_key_checksum = hashlib.sha1(public_key.encode()).hexdigest()
                        key_file = "%s/%s.pubkey" % (self.keys_path, uri)
                        fd = open(key_file, "wb+")
                        fd.write(public_key.encode())
                        fd.close()
                        #BlinkLogger().log_info(u"Public key for %s was saved to %s" % (uri, key_file))
                        # The banner these three were built for is commented
                        # out below; sender_identity does not exist in this
                        # scope, so building the subtitle raised NameError on
                        # every public key that arrived.
                        self.notification_center.post_notification('PGPPublicKeyReceived', sender=account, data=NotificationData(uri=uri, key=public_key))

                        self.saveContact(uri, {'public_key': key_file, 'public_key_checksum': public_key_checksum})
                    else:
                         BlinkLogger().log_info(u"No public key detected in the payload")

                elif (content_type.startswith('text/')
                        or content_type in (LOCATION_CONTENT_TYPE, LEGACY_LOCATION_CONTENT_TYPE)
                        or content_type in FILE_TRANSFER_CONTENT_TYPES):
                    # application/sylk-location-sharing carries the
                    # one-shot / live / meet coordinate ticks and the
                    # lifecycle signals; application/sylk-message-metadata
                    # is its legacy predecessor (action='location') and is
                    # still read. Treat both like a regular text message at
                    # the persistence layer; the renderer branches on
                    # content_type to draw a location bubble or post a
                    # system note.
                    if msg['direction'] == 'incoming':
                        sync_contacts.add(msg['contact'])
                        # Counted for the banner, so it must count what the
                        # user would call a message. Every entry, as this
                        # first did, turns one live-location share into
                        # "26 messages received while you were away".
                        try:
                            notable = self._journal_message_is_notable(account, msg)
                        except Exception:
                            notable = False     # never lose the sync over a count
                        if notable:
                            sync_incoming[msg['contact']] = \
                                sync_incoming.get(msg['contact'], 0) + 1
                        self.syncIncomingMessage(account, msg, cursor)
                    elif msg['direction'] == 'outgoing':
                        sync_contacts.add(msg['contact'])
                        self.syncOutgoingMessage(account, msg, cursor)
                else:
                    # No branch claims this type -- so it is STORED, not
                    # dropped. A journal entry is the only copy this
                    # device will ever be offered: the server hands it
                    # over once, the cursor moves past it, and it is
                    # never sent again. Discarding one because this build
                    # has no renderer for it means the feature that
                    # understands it later finds nothing to render, and
                    # the only way back is a full resync -- which is
                    # exactly how every recording's waveform was lost.
                    #
                    # Still counted, so the log says what arrived that
                    # nothing yet handles.
                    sync_unhandled[content_type] = sync_unhandled.get(content_type, 0) + 1
                    self._persist_unhandled_journal_message(account, msg)
                    
            except Exception as e:
                BlinkLogger().log_error('Failed to sync message %s' % msg)
                import traceback
                traceback.print_exc()

        elapsed = (datetime.datetime.now() - started).total_seconds()
        working = max(elapsed - slept, 0.0)
        BlinkLogger().log_info(
            'Journal %s applied: %d entries in %.1fs (%.1fs working, %.1fs throttled%s)'
            % (label or 'page', total, elapsed, working, slept,
               ', %.0f entries/s' % (total / working) if working > 0.001 else ''))
        return last_message_id, sync_contacts, sync_incoming, sync_summary, sync_unhandled

    @objc.python_method
    def _finishJournalApply(self, account, sync_contacts, incoming_counts=None):
        # A handful of senders is worth naming: the point of the banner is to
        # tell the user WHO wrote while they were away, and "From 2 contacts"
        # makes them open the app to find out. Past a few, one banner per
        # sender stops being information and becomes a stack of banners, so
        # the consolidated count takes over.
        # Only what the user would call a message. A catch-up can be
        # hundreds of entries -- trail ticks, receipts, reply links, key
        # updates -- and none of them is news. With nothing notable in the
        # batch there is no banner at all, rather than one announcing
        # traffic the user cannot see and did not ask about.
        senders = incoming_counts or {}
        if not senders:
            if sync_contacts:
                BlinkLogger().log_debug('Journal sync carried nothing notable; no banner')
        elif len(senders) < self.NAMED_OFFLINE_NOTIFICATION_LIMIT:
            delegate = NSApp.delegate()
            for uri, count in sorted(senders.items(), key=lambda item: -item[1]):
                nc_title, nc_icon = self.notificationIdentity(uri)
                if count == 1:
                    nc_body = NSLocalizedString("1 message received while you were away", "Label")
                else:
                    nc_body = NSLocalizedString("%d messages received while you were away" % count, "Label")
                delegate.notify_new_message(nc_title, nc_body, None, uri=uri, icon=nc_icon)
        else:
            nc_title = NSLocalizedString("Offline messages received", "Label")
            count = len(senders)
            if count == 1:
                nc_body = NSLocalizedString("From 1 contact", "Label")
            else:
                nc_body = NSLocalizedString("From %d contacts" % count, "Label")
            NSApp.delegate().notify_new_message(nc_title, nc_body)

        for uri in sync_contacts:
            self.ensureMessagesGroupContains(uri)

        # Post-sync history refresh — limit it to the viewer the
        # user is actually looking at right now. The live path
        # (_presentJournalIncomingMessage -> gotMessage) already
        # appends new messages to every open viewer as the burst
        # is processed, so non-focused tabs aren't going to miss
        # anything — they just don't get a re-render. When the
        # user clicks a stale tab next, replay_history runs as
        # part of the existing chat-view-load flow and the panel
        # catches up.
        #
        # Old behaviour (one scroll_back_in_time per contact in
        # sync_contacts × per viewer) is what stacked thousands
        # of run_in_gui_thread render calls onto the main thread
        # at the tail end of a big sync and pushed the app into
        # a 1–2 minute beachball.
        for window in self.windows:
            if not window.window().isVisible():
                continue
            focused = window.selectedSessionController()
            if focused is None or focused.account != account:
                continue
            if focused.remote_uri in sync_contacts:
                BlinkLogger().log_info('Refresh focused viewer for %s' % focused.remote_uri)
                focused.scroll_back_in_time()

        self.addContactsToMessagesGroup()

        if self._order_changed_during_bulk:
            self._order_changed_during_bulk = False
            self._postConversationOrderChanged(None)

        self._flushUnreadChanged()

    @objc.python_method
    @run_in_thread('sms_sync')
    def syncConversations(self, account):
        """Bring the local history up to date with the server journal.

        Every outcome leaves a line in the log. It used to be possible for a
        sync to run, find nothing and say nothing -- indistinguishable from
        never having been called at all -- which made "did it sync?" a
        question you could only answer by reading this file.
        """
        if not account.sms.enable_replication:
            self._logSyncSkipped(account, 'replication is disabled for this account')
            return

        if not account.sms.history_token:
            self._logSyncSkipped(account, 'there is no history token yet; requesting one')
            self.requestSyncToken(account)
            return

        if not account.sms.history_url:
            self._logSyncSkipped(account, 'there is no history url yet')
            return

        try:
            self.syncConversationsInProgress[account.id]
        except KeyError:
            self.syncConversationsInProgress[account.id] = True
        else:
            # Two callers race on a fresh token: the api-token handler calls
            # this directly, and the settings change that same handler causes
            # calls it again. Expected, and not worth an info line.
            BlinkLogger().log_debug('Journal sync for %s is already running' % account.id)
            return

        self.sync_skip_logged.pop(account.id, None)
        cursor = account.sms.history_last_id
        started = time.time()
        BlinkLogger().log_info('Journal sync starting for %s from %s (%s)'
                               % (account.id, account.sms.history_url,
                                  'after %s' % cursor if cursor
                                  else 'since %s' % self.journalSinceWindow()))

        pages = entries = 0
        # Released in `finally`, not per exit path: previously a non-401 HTTP
        # error returned with this flag still set, wedging journal sync for the
        # account until the app restarted.
        try:
            pages, entries = self._downloadJournal(account)
            self._applyCachedJournals(account)
        finally:
            try:
                del self.syncConversationsInProgress[account.id]
            except KeyError:
                pass

        if pages:
            BlinkLogger().log_info('Journal sync for %s finished in %.1fs: %d page(s), %d entries'
                                   % (account.id, time.time() - started, pages, entries))
        else:
            BlinkLogger().log_info('Journal sync for %s finished in %.1fs: the server had nothing new'
                                   % (account.id, time.time() - started))

        if not cursor:
            # There was no cursor when this started, so this was the account's
            # first sync on this device -- the same condition sylk mobile uses
            # for afterFirstSync (no last message and no stored sync id).
            self.announceActivation(account)

    @objc.python_method
    def announceActivation(self, account):
        """Tell the account's other devices that this one has joined.

        A plain-text note to ourselves, which the journal replicates to every
        other device, matching what sylk mobile sends from afterFirstSync:
        "Account activated on <user agent>".

        Sent once per account, not once per launch. The first sync is the
        right moment because the note is an addition to a conversation the
        device has just finished reading -- announcing before the sync would
        put it above history that had not arrived yet.
        """
        if account.sms.activation_announced:
            return

        # Recorded before sending rather than after. The send is asynchronous
        # and has no success callback here, so a failure that is retried on
        # the next launch is the lesser problem: an unrecorded success would
        # announce this device again on every start.
        account.sms.activation_announced = True
        account.save()

        text = 'Account activated on %s' % SIPSimpleSettings().user_agent
        BlinkLogger().log_info('Announcing activation of %s: %s' % (account.id, text))
        self.sendMessage(account, text, 'text/plain')

    @objc.python_method
    def _logSyncSkipped(self, account, reason):
        """Say why a sync did not run -- once per reason, not once per refresh.

        syncConversations runs on every registration refresh, so logging every
        skip at info drowned the log. That is why these lines were demoted to
        debug, and why the log stopped being able to answer whether a sync had
        happened. Logging only when the reason CHANGES keeps the first
        occurrence and the recovery, and stays quiet in between.
        """
        if self.sync_skip_logged.get(account.id) == reason:
            BlinkLogger().log_debug('Journal sync skipped for %s: %s' % (account.id, reason))
            return
        self.sync_skip_logged[account.id] = reason
        BlinkLogger().log_info('Journal sync skipped for %s: %s' % (account.id, reason))


    @objc.python_method
    def _logJournalSummary(self, account, sync_summary, unhandled=None, max_contacts=50):
        """Log what a journal sync actually delivered, per contact.

        Counts every journal entry, control types included (imdn,
        sylk-contact-update, pgp keys ...), because "why did this sync move
        last_message_id but show me nothing" is usually answered by seeing
        that the burst was all IMDN receipts.
        """
        if not sync_summary:
            BlinkLogger().log_info('Journal sync for %s: no entries' % account.id)
            return

        total = sum(sum(counts.values()) for counts in sync_summary.values())
        BlinkLogger().log_info('Journal sync for %s: %d entries from %d contacts'
                               % (account.id, total, len(sync_summary)))

        ordered = sorted(sync_summary.items(),
                         key=lambda item: (-sum(item[1].values()), item[0]))
        for contact, counts in ordered[:max_contacts]:
            types = ', '.join('%s=%d' % (content_type, number)
                              for content_type, number
                              in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
            BlinkLogger().log_info('  %s: %d (%s)' % (contact, sum(counts.values()), types))

        remaining = len(ordered) - max_contacts
        if remaining > 0:
            BlinkLogger().log_info('  ... and %d more contacts not listed' % remaining)

        if unhandled:
            dropped = sum(unhandled.values())
            types = ', '.join('%s=%d' % (content_type, number)
                              for content_type, number
                              in sorted(unhandled.items(), key=lambda kv: (-kv[1], kv[0])))
            BlinkLogger().log_info('  UNHANDLED: %d of %d entries dropped, no handler for: %s'
                                   % (dropped, total, types))

    @objc.python_method
    def saveContact(self, uri, data={}):
        if self.illegal_uri(uri):
            return

        contact = self.getContact(uri)
        if contact is not None:
            attrs = ('public_key', 'public_key_checksum', 'name', 'organization')
            for a in attrs:
                try:
                    value = data[a]
                except KeyError:
                    pass
                else:
                    setattr(contact, a, value)
            contact.save()
        else:
            BlinkLogger().log_info("No contact found to save the public key for %s" % uri)

    @objc.python_method
    def illegal_uri(self, uri):
        if '@videoconference.' in uri:
            return True

        if '@guest.' in uri:
            return True

        try:
            SIPURI.parse('sip:%s' % uri)
        except:
            return True

        return False

    @objc.python_method
    def getContact(self, uri, addGroup=False):
        if self.illegal_uri(uri):
            return None

        blink_contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(uri)
        if blink_contact is not None:
            contact = blink_contact.contact
        else:
            # The model's groupsList only iterates contacts that are
            # already filed in a group. A contact created earlier in
            # this same burst (or from a parallel incoming message) is
            # saved to AddressbookManager but won't appear in any group
            # until addContactsToMessagesGroup() commits, which happens
            # after this lookup returns. Scan the addressbook directly
            # so that gap doesn't generate a duplicate Contact.
            contact = self._findContactByCanonicalURI(uri)
            if contact is None:
                BlinkLogger().log_info('Adding contact for %s' % uri)
                contact = NSApp.delegate().contactsWindowController.model.addContactForUri(uri)
                self.new_contacts.add(contact)

        if addGroup:
            self.addContactsToMessagesGroup()

        return contact

    @objc.python_method
    def _findContactByCanonicalURI(self, uri):
        """Return an existing Contact whose canonical URI matches `uri`,
        or None. Walks AddressbookManager.get_contacts() directly so we
        catch contacts that exist in storage but haven't been filed into
        the model's groupsList yet (the duplication race in getContact).
        Linear scan: with a few hundred contacts this is far cheaper
        than the cost of the duplicate it prevents.
        """
        canonical = self._canonical_uri(uri)
        if not canonical:
            return None
        try:
            contacts = AddressbookManager().get_contacts()
        except Exception:
            return None
        for c in contacts:
            try:
                for u in c.uris:
                    if self._canonical_uri(u.uri) == canonical:
                        return c
            except Exception:
                continue
        return None

    @objc.python_method
    def addContactsToMessagesGroup(self):
        if len(self.new_contacts) == 0:
            return

        group_id = '_messages'
        try:
            group = next((group for group in AddressbookManager().get_groups() if group.id == group_id))
        except StopIteration:
            try:
                group = Group(id=group_id)
            except DuplicateIDError as e:
                return
            else:
                group.name = 'Messages'
                group.position = 0
                group.expanded = True
                # Position 0 is not enough on its own -- every group with an
                # unset position is also given 0, so the new group would be
                # filed among them rather than above them. Ask the model to
                # promote it when it activates; it is a one-off, so reordering
                # the list afterwards sticks.
                try:
                    NSApp.delegate().contactsWindowController.model.promoteGroupOnActivation(group_id)
                except Exception as e:
                    BlinkLogger().log_error('Cannot promote the Messages group: %s' % e)

        # Only save when membership actually changed. This runs at the end
        # of every journal sync, and an unconditional save posts a group
        # change notification that rebuilds the group in the model each time.
        existing = set(group.contacts)
        added = 0
        for contact in self.new_contacts:
            if contact in existing:
                continue
            group.contacts.add(contact)
            added += 1

        self.new_contacts = set()
        if added:
            BlinkLogger().log_info('Added %d contact(s) to the Messages group' % added)
            group.save()

    @objc.python_method
    def ensureMessagesGroupContains(self, uri):
        """Make sure `uri` has a contact and that it is filed under Messages.

        Nothing else does this any more. A contact used to reach the group
        as a side effect of opening a conversation, and journalled messages
        deliberately never open one -- so a conversation that arrived purely
        through replication had no row to appear in, no matter how many
        unread messages it carried.

        An already-known contact is filed too: being in some other group is
        no reason to be missing from the one that lists your chats.
        """
        if self.illegal_uri(uri):
            return None
        try:
            contact = self.getContact(uri)          # creates it when absent
            if contact is None:
                return None
            self.new_contacts.add(contact)
            return contact
        except Exception as e:
            BlinkLogger().log_error('Cannot file %s under Messages: %s' % (uri, e))
            return None

    @objc.python_method
    def _canonical_uri(self, raw_uri):
        """Normalize a SIP URI for duplicate-detection purposes.

        Strips 'sip:'/'sips:' scheme, ';parameters', URL-encoded display
        parts and surrounding whitespace, then lower-cases the result so
        case-only variants collapse to one key. The bare result is
        intentionally left as 'user@host[:port]' — port is preserved
        because Blink stores port-suffixed URIs distinctly today, and
        we want the audit to *show* that as a duplicate rather than
        silently merge it.
        """
        if raw_uri is None:
            return ''
        s = str(raw_uri).strip()
        # strip scheme
        for scheme in ('sips:', 'sip:'):
            if s.lower().startswith(scheme):
                s = s[len(scheme):]
                break
        # strip parameters (everything after first ';')
        if ';' in s:
            s = s.split(';', 1)[0]
        # strip headers (anything after '?')
        if '?' in s:
            s = s.split('?', 1)[0]
        return s.lower().strip()

    @objc.python_method
    def _describe_public_key(self, public_key):
        """(names_itself_as, fingerprint) for a key blob, or (None, None).

        The user ids are what make a mismatch warning actionable: without
        them the log says a key looks wrong but not whose it is, which is
        the difference between spotting a misfiled key and guessing.
        """
        try:
            key, _ = pgpy.PGPKey.from_blob(public_key)
        except Exception as e:
            BlinkLogger().log_debug('Cannot parse a received public key: %s' % e)
            return None, None
        try:
            names = [str(uid).strip() for uid in key.userids]
            names = [name for name in names if name] or ['(no user id)']
            fingerprint = str(key.fingerprint).replace(' ', '')[-16:]
        except Exception:
            return None, None
        return names, fingerprint

    @objc.python_method
    def _warn_if_key_mismatched(self, public_key, uri):
        """Flag a key about to be filed under an address it does not claim.

        A key legitimately carries an email that differs from the SIP
        address, so this warns rather than refuses -- but it is the cheapest
        signal that a key is heading for the wrong contact, and that bug
        destroys the real key silently.
        """
        names, fingerprint = self._describe_public_key(public_key)
        checksum = public_key_short_checksum(public_key)
        if names is None:
            BlinkLogger().log_info('Public key stored for %s, checksum %s (unreadable key)'
                                   % (uri, checksum))
            return

        BlinkLogger().log_info(
            'Public key stored for %s: checksum %s, fingerprint %s, identifies as %s'
            % (uri, checksum, fingerprint, ', '.join(names)))

        target = self._canonical_uri(uri)
        if not target or any(target in name.lower() for name in names):
            return
        if names == ['(no user id)']:
            # A key with no user id at all cannot name anybody; that is not
            # evidence of a misfiled key, so it does not warrant a warning.
            return
        BlinkLogger().log_warning(
            'The public key being saved for %s identifies itself as %s '
            '(checksum %s). Saving anyway -- a key can legitimately name a '
            'different address, but if that is not this contact then it is '
            'the wrong key.' % (uri, ', '.join(names), checksum))

    @objc.python_method
    def _isUserRenamedContact(self, contact):
        """A contact is considered 'user-renamed' (and therefore worth
        keeping over auto-created clones) when its display name is
        present and doesn't equal any of its URIs. The auto-create path
        in addContactForUri() defaults `name = uri`, so anything where
        name != uri is a sign the user touched it.
        """
        try:
            name = (getattr(contact, 'name', '') or '').strip()
            if not name:
                return False
            lname = name.lower()
            for u in contact.uris:
                if lname == self._canonical_uri(u.uri):
                    return False
                if lname == str(u.uri).lower().strip():
                    return False
            return True
        except Exception:
            return False

    @objc.python_method
    def mergeMessagesGroupDuplicates(self):
        """Walk the 'Messages' (_messages) group and merge duplicate
        Contacts that share a canonical URI. Survivor is chosen as:
            1. the contact with a user-edited display name (name != uri)
            2. otherwise the oldest contact (lowest id, lexicographic)
        Non-survivors are removed from the group and deleted from the
        addressbook. Chat history is keyed by SIP URI (not contact id)
        and the URIs are byte-identical across a cluster, so timelines
        are inherited automatically by the survivor.

        Returns the number of contacts deleted.
        """
        log = BlinkLogger().log_info
        group_id = '_messages'

        try:
            group = next(g for g in AddressbookManager().get_groups() if g.id == group_id)
        except StopIteration:
            log("Messages group merge: no '_messages' group exists yet")
            return 0

        # Cluster contacts by canonical URI, exactly like the audit.
        by_key = {}
        for c in list(group.contacts):
            try:
                uris = list(c.uris)
            except Exception:
                uris = []
            keys = set()
            for u in uris:
                try:
                    k = self._canonical_uri(u.uri)
                except Exception:
                    k = ''
                if k:
                    keys.add(k)
            if not keys:
                # No URIs at all — leave it alone, we can't merge by key
                continue
            for k in keys:
                by_key.setdefault(k, []).append(c)

        addressbook_manager = AddressbookManager()
        deleted_total = 0
        clusters_merged = 0
        try:
            with addressbook_manager.transaction():
                for key, members in by_key.items():
                    unique = {c.id: c for c in members}
                    if len(unique) <= 1:
                        continue

                    renamed = [c for c in unique.values()
                               if self._isUserRenamedContact(c)]
                    if renamed:
                        survivor = min(renamed, key=lambda c: c.id)
                    else:
                        survivor = min(unique.values(), key=lambda c: c.id)

                    losers = [c for c in unique.values() if c.id != survivor.id]
                    log("MERGE canonical=%r keep id=%s name=%r drop=%d" % (
                        key, survivor.id, getattr(survivor, 'name', ''),
                        len(losers)))

                    for c in losers:
                        try:
                            log("  -> delete id=%s name=%r" % (
                                c.id, getattr(c, 'name', '')))
                            try:
                                group.contacts.discard(c)
                            except Exception:
                                # Some Group implementations expose contacts
                                # as a list rather than a set.
                                try:
                                    group.contacts.remove(c)
                                except Exception:
                                    pass
                            c.delete()
                            deleted_total += 1
                        except Exception as e:
                            log("  -> delete failed id=%s: %s" % (c.id, e))

                    clusters_merged += 1

                if deleted_total:
                    try:
                        group.save()
                    except Exception as e:
                        log("Failed to save _messages group after merge: %s" % e)
        except Exception as e:
            log("Messages group merge transaction failed: %s" % e)
            return 0

        if deleted_total:
            log("Messages group merge: %d cluster(s), deleted %d duplicate "
                "contact(s)" % (clusters_merged, deleted_total))
        return deleted_total

    @objc.python_method
    def _decrypt_pgp_for_account(self, account_id, body):
        """Decrypt a PGP-armoured body using the account's private key.

        Returns the decoded plaintext on success, ``None`` if there is
        no key or decryption fails. Reuses the per-account ``private_keys``
        cache that handle_contacts_queue already populates so we don't
        re-read the key file on every journal entry.
        """
        try:
            private_key = self.private_keys[account_id]
        except KeyError:
            private_key_path = "%s/%s.privkey" % (self.keys_path, account_id)
            try:
                private_key, _ = pgpy.PGPKey.from_file(private_key_path)
            except Exception as e:
                BlinkLogger().log_error('Cannot import PGP private key from %s: %s' % (private_key_path, str(e)))
                self.private_keys[account_id] = None
                return None
            self.private_keys[account_id] = private_key

        if not private_key:
            return None

        try:
            pgpMessage = pgpy.PGPMessage.from_blob(body.strip())
            decrypted_message = private_key.decrypt(pgpMessage)
        except (pgpy.errors.PGPDecryptionError, pgpy.errors.PGPError):
            return None

        return pgp_plaintext(decrypted_message)

    @objc.python_method
    def _decode_location_payload(self, account_id, content, metadata=None,
                                 content_type=LOCATION_CONTENT_TYPE):
        """Decode a location message into its normalised payload, or None.

        Handles all three shapes this client can meet on the wire:

          * **v2** — the cleartext lifecycle envelope arrives in
            ``metadata`` (the CPIM ``agp.Metadata`` body header live, the
            journal's metadata column on catch-up) and ``content`` is the
            armoured coordinates, or the empty string for a
            coordinate-free signal;
          * **v1** — the whole envelope is the JSON body, coordinates
            nested under ``value``;
          * the legacy ``application/sylk-message-metadata`` tick with
            ``action == 'location'``.

        Coordinates are decrypted with the account's private key when
        available; a coordinate tick we cannot decrypt returns None,
        while a signal (which has nothing to decrypt) does not.
        """
        decrypt = lambda blob: self._decrypt_pgp_for_account(account_id, blob)
        try:
            return location_payload(content, metadata, decrypt=decrypt,
                                    content_type=content_type)
        except Exception as e:
            BlinkLogger().log_debug('Failed to decode location payload: %s' % str(e))
            return None

    @objc.python_method
    def _is_silent_location_tick(self, account_id, content, metadata=None,
                                 content_type=LOCATION_CONTENT_TYPE):
        """True iff this location message must not bump the badge / raise.

        Only a coordinate **origin** (one-shot, live start, meet start,
        meet invite) opens a bubble and counts as a new message. Trail
        ticks merely move a pin that is already on screen, and the
        coordinate-free handshake / teardown signals leave a system-note
        breadcrumb rather than a chat message. If we cannot decode the
        payload at all we conservatively return False — failing to
        suppress the raise is preferable to suppressing it for a genuine
        first share.
        """
        payload = self._decode_location_payload(account_id, content, metadata, content_type)
        if payload is None:
            return False
        return not is_notable_action(payload)

    @objc.python_method
    def cachedRoutes(self, key):
        return self.route_cache.get(key)

    @objc.python_method
    def storeRoutes(self, key, routes):
        if not routes:
            return
        if key not in self.route_cache:
            BlinkLogger().log_debug('Route cached for %s' % (key,))
        self.route_cache[key] = routes

    @objc.python_method
    def invalidateRoutes(self, key, reason=''):
        if self.route_cache.pop(key, None) is not None:
            BlinkLogger().log_info('Route cache dropped for %s%s'
                                   % (key[1] if len(key) > 1 else key,
                                      (' (%s)' % reason) if reason else ''))

    @objc.python_method
    def _normalized_timestamp(self, value):
        """Naive UTC datetime for anything a message timestamp can be.

        Values reach us as ISOTimestamp, as datetime and as the string form
        SQLite hands back, some tz-aware and some not. Comparing those
        directly raises, so everything is flattened to one shape here.
        """
        if value is None:
            return None
        if isinstance(value, datetime.datetime):
            stamp = value
        else:
            text = str(value).strip().replace('T', ' ')
            if not text:
                return None
            stamp = None
            for fmt in ('%Y-%m-%d %H:%M:%S.%f%z', '%Y-%m-%d %H:%M:%S%z',
                        '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S',
                        '%Y-%m-%d %H:%M'):
                try:
                    stamp = datetime.datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if stamp is None:
                return None
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return stamp

    @objc.python_method
    @run_in_green_thread
    def loadLastMessageTimes(self):
        """Seed the conversation order from history, off the GUI thread.

        Green, not a plain worker thread: ChatHistory hands its results back
        through block_on, which is an eventlib primitive and only works from
        a green thread. Under run_in_thread it raised, the seed came back
        empty, and the Messages group quietly fell back to alphabetical.
        """
        try:
            stored = self.history.last_message_times()
        except Exception as e:
            BlinkLogger().log_error('Cannot read the last message times: %s' % e)
            return
        BlinkLogger().log_debug('History knows %d conversation(s) with messages'
                                % len(stored))
        loaded = 0
        for uri, stamp in stored.items():
            key = self._canonical_uri(uri)
            when = self._normalized_timestamp(stamp)
            if not key or when is None:
                continue
            if self.last_message_times.get(key) is None or when > self.last_message_times[key]:
                self.last_message_times[key] = when
                loaded += 1
        BlinkLogger().log_info('Conversation order seeded from %d conversation(s)' % loaded)
        if loaded:
            self._postConversationOrderChanged(None)

    @objc.python_method
    @run_in_green_thread
    def loadUnreadCounts(self):
        """Restore the unread badges from the table.

        Green, like loadLastMessageTimes and for the same reason: the
        history answers through block_on, which quietly returns nothing
        when it is called from anywhere else.
        """
        try:
            stored = self.history.unread_counts()
        except Exception as e:
            BlinkLogger().log_error('Cannot read the unread counts: %s' % e)
            return

        counts = {}
        for uri, count in stored.items():
            key = self._canonical_uri(uri)
            if key and count > 0:
                counts[key] = counts.get(key, 0) + count
        if not counts:
            BlinkLogger().log_debug('No unread messages were waiting')
            return
        self._applyRestoredUnreadCounts(counts)

    @objc.python_method
    @run_in_gui_thread
    def _applyRestoredUnreadCounts(self, counts):
        """Put the restored badges on screen, on the GUI thread.

        The counting happens in a green thread because that is the only
        place the history will answer, but filing contacts into the
        Messages group is address-book and AppKit work. Doing that from
        the green thread was corrupting objects the run loop later
        released, which surfaced as a crash inside a timer with nothing to
        do with any of this.
        """
        restored = 0
        for key, count in counts.items():
            self.unread_counts[key] = self.unread_counts.get(key, 0) + count
            restored += count

        BlinkLogger().log_info('Restored %d unread message(s) in %d conversation(s)'
                               % (restored, len(counts)))
        for key in counts:
            self.ensureMessagesGroupContains(key)
        self.addContactsToMessagesGroup()
        for key, count in counts.items():
            self._postUnreadChanged(key, self.unread_counts.get(key, count))

    @objc.python_method
    def noteMessageTime(self, remote_uri, timestamp):
        """Record when a conversation last carried a message."""
        key = self._canonical_uri(remote_uri)
        when = self._normalized_timestamp(timestamp)
        if not key or when is None:
            return
        known = self.last_message_times.get(key)
        if known is not None and when <= known:
            return
        self.last_message_times[key] = when
        self._postConversationOrderChanged(key)

    @objc.python_method
    def lastMessageTimeForURI(self, remote_uri):
        return self.last_message_times.get(self._canonical_uri(remote_uri))

    @objc.python_method
    def _postConversationOrderChanged(self, key):
        # A journal apply walks thousands of messages; announcing each one
        # would re-sort and redraw the contact list thousands of times. The
        # burst collapses into a single notification when the apply ends.
        if self._journal_bulk:
            self._order_changed_during_bulk = True
            return
        self.notification_center.post_notification(
            'BlinkConversationOrderChanged', sender=self,
            data=NotificationData(key=key))

    @objc.python_method
    def fileTransferBaseURL(self, account):
        """Where this account uploads files, or None if we cannot tell.

        Three sources, in order of how much they can be trusted: what a
        received transfer told us (the server's own URL, stored on the
        account the first time one arrives), what the journal URL implies,
        and nothing. Deriving from the journal URL is a guess about one
        path segment, so the guess is logged -- but only when the answer
        changes, since this is called per transfer and per composer and was
        repeating the same line four times in a row.
        """
        try:
            stored = account.sms.file_transfer_url
        except AttributeError:
            stored = None
        if stored:
            return str(stored)

        try:
            history_url = str(account.sms.history_url or '')
        except AttributeError:
            history_url = ''
        if not history_url:
            return None

        root = history_url.split('/messages')[0].rstrip('/')
        if root == history_url.rstrip('/'):
            # No /messages to cut: the journal URL is shaped in a way this
            # derivation does not know, and inventing a path from it would
            # be worse than saying so.
            self._logDerivedTransferURL(account, None,
                                        'Cannot derive the file transfer URL from %s; it will be '
                                        'learned from the first file received' % history_url)
            return None
        derived = root + FILE_TRANSFER_PATH
        self._logDerivedTransferURL(account, derived,
                                    'File transfer URL derived from the journal URL: %s' % derived)
        return derived

    @objc.python_method
    def _logDerivedTransferURL(self, account, derived, message):
        """Log a derived transfer URL once, not on every derivation."""
        if self.transfer_url_logged.get(account.id, False) == derived:
            return
        self.transfer_url_logged[account.id] = derived
        BlinkLogger().log_info(message)

    @objc.python_method
    def noteFileTransferURL(self, account, body):
        """Learn the endpoint from a transfer that has just arrived.

        Cheap enough to call on every file-transfer message: it returns at
        the first sight of a stored value, which is the second message
        onwards.
        """
        try:
            if account.sms.file_transfer_url:
                return
        except AttributeError:
            return

        meta = file_transfer_envelope(body)
        if not meta:
            return
        base = base_url_from_transfer(meta.get('url'))
        if not base:
            return
        try:
            account.sms.file_transfer_url = base
            account.save()
            BlinkLogger().log_info('File transfer URL for %s learned from an incoming '
                                   'transfer: %s' % (account.id, base))
        except Exception as e:
            BlinkLogger().log_error('Cannot remember the file transfer URL: %s' % e)

    @objc.python_method
    def purgeConversationsForURIs(self, uris, reason='contact deleted'):
        """Erase every trace of one or more addresses.

        Called when a contact goes and its addresses are left belonging to
        nobody. Everything filed under the address goes with it: the open
        conversation, its stored messages, the files downloaded from it,
        the public key held for it, and its place in the conversation
        order. The caller decides which addresses are actually orphaned --
        an address shared with another contact must survive, because the
        messages under it belong to that contact too.
        """
        targets = []
        for uri in uris:
            key = self._canonical_uri(uri)
            if key and key not in targets:
                targets.append(key)
        if not targets:
            return

        BlinkLogger().log_info('Purging %d conversation(s) (%s): %s'
                               % (len(targets), reason, ', '.join(targets)))

        for uri in targets:
            self.closeConversationForURI(uri)

        for uri in targets:
            self.last_message_times.pop(uri, None)
            if self.unread_counts.pop(uri, None):
                self._postUnreadChanged(uri, 0)
            self._postConversationOrderChanged(uri)

        self._purgeStoredData(targets)

    @objc.python_method
    def closeConversationForURI(self, uri):
        """Take a conversation off the screen, wherever it is being shown."""
        key = self._canonical_uri(uri)
        if not key:
            return False
        closed = False
        for viewer in list(self.allViewers()):
            if self._canonical_uri(getattr(viewer, 'remote_uri', '')) != key:
                continue
            host = self.windowForViewer(viewer)
            if host is not None:
                try:
                    host.removeViewer_(viewer)
                except Exception as e:
                    BlinkLogger().log_error('Cannot close the conversation with %s: %s'
                                            % (key, e))
            self.viewer_hosts.pop(viewer, None)
            closed = True
        return closed

    @objc.python_method
    @run_in_green_thread
    def _purgeStoredData(self, uris):
        """The part that touches the disc, off the GUI thread.

        Green rather than a plain worker: ChatHistory answers through
        block_on, which only works from a green thread.
        """
        from FileTransferCache import FileTransferCache
        for uri in uris:
            try:
                self.history.delete_messages(remote_uri=uri)
            except Exception as e:
                BlinkLogger().log_error('Cannot delete the messages of %s: %s' % (uri, e))

            try:
                removed = FileTransferCache().purge_peer(uri)
                if removed:
                    BlinkLogger().log_info('Deleted %d downloaded file(s) from %s'
                                           % (removed, uri))
                FileTransferCache().forget_peer(uri)
            except Exception as e:
                BlinkLogger().log_error('Cannot delete the files of %s: %s' % (uri, e))

            key_file = "%s/%s.pubkey" % (self.keys_path, uri)
            try:
                if os.path.exists(key_file):
                    os.remove(key_file)
                    BlinkLogger().log_info('Deleted the public key held for %s' % uri)
            except OSError as e:
                BlinkLogger().log_error('Cannot delete %s: %s' % (key_file, e))

    @objc.python_method
    def noteUnreadMessage(self, remote_uri, delta=1):
        """Bump the unread count for a conversation and announce it."""
        key = self._canonical_uri(remote_uri)
        if not key:
            return 0
        count = self.unread_counts.get(key, 0) + delta
        self.unread_counts[key] = count
        self._postUnreadChanged(key, count)
        return count

    @objc.python_method
    def clearUnreadMessages(self, remote_uri):
        """Drop the unread badge for a conversation.

        Returns True when there was actually something to clear, which is
        the caller's cue to tell the account's other devices. Deliberately
        does NOT announce by itself: applyConversationRead clears the badge
        too, and a marker echoed straight back to the device that sent it
        is a loop.
        """
        key = self._canonical_uri(remote_uri)
        # The rows are marked read whether or not a counter was standing:
        # the counter is this session's view of what the table already
        # knows, and the two have to agree or the badge comes back on the
        # next launch.
        self.history.mark_conversation_read(remote_uri=str(remote_uri))
        if self.unread_counts.pop(key, None) is None:
            return False
        self._postUnreadChanged(key, 0)
        return True

    @objc.python_method
    def unreadCountForURI(self, remote_uri):
        return self.unread_counts.get(self._canonical_uri(remote_uri), 0)

    @objc.python_method
    def _postUnreadChanged(self, key, count):
        # A journal apply can walk thousands of messages; announcing each one
        # would redraw the contact list thousands of times. The burst
        # collapses into one notification per affected contact at the end,
        # which is a handful.
        if self._journal_bulk:
            self._unread_changed_during_bulk.add(key)
            return
        total = sum(self.unread_counts.values())
        self.notification_center.post_notification(
            'BlinkUnreadMessageCountChanged', sender=self,
            data=NotificationData(key=key, count=count, total=total))

    @objc.python_method
    def _flushUnreadChanged(self):
        keys = self._unread_changed_during_bulk
        self._unread_changed_during_bulk = set()
        if not keys:
            return
        BlinkLogger().log_info('Journal sync left unread messages for %d contact(s)' % len(keys))
        for key in keys:
            self._postUnreadChanged(key, self.unread_counts.get(key, 0))

    @objc.python_method
    def _persistLiveMessage(self, account, remote_uri, msgid, call_id, direction,
                            content, content_type, timestamp, metadata=None,
                            unread=False):
        """Store a live message without creating a conversation.

        Mirrors _persist_journal_message: location payloads fold into their
        origin row, everything else is a plain insert. A PGP body is stored
        as ciphertext exactly as the journal path does -- history replay
        decrypts it when the user actually opens the conversation.

        Returns True when a new row was added (i.e. something the user has
        not seen), False for updates and dropped payloads.
        """
        body = content.decode() if isinstance(content, bytes) else (content or '')
        # A teardown signal or a trail tick is the same share still
        # running; only the start of one is a new thing said.
        stamps_conversation_time = True

        # Control plane, not a message: no row, no unread count, no banner.
        if is_otr_wire_text(body):
            BlinkLogger().log_debug('Dropped OTR traffic from %s (no conversation open)'
                                    % remote_uri)
            return False

        encryption = ''
        stripped = body.strip()
        if stripped.startswith('-----BEGIN PGP MESSAGE-----') and stripped.endswith('-----END PGP MESSAGE-----'):
            encryption = 'pgp_encrypted'

        if content_type in FILE_TRANSFER_CONTENT_TYPES:
            self.noteFileTransferURL(account, body)

        if content_type in (LOCATION_CONTENT_TYPE, LEGACY_LOCATION_CONTENT_TYPE):
            # A reply link and a recording's waveform share the legacy
            # location content type but are neither ticks nor bubbles.
            # Stored verbatim, because each is the ONLY record of
            # something a message needs: losing the first turns a reply
            # back into an unrelated remark, and losing the second leaves
            # a recording with a bare scrub bar for ever -- the server
            # relays a fixed field set for a transfer and drops the rest,
            # so there is nowhere else for the waveform to come from.
            if reply_metadata(body) is not None or peaks_metadata(body) is not None:
                self.history.add_message(
                    msgid, 'sms', str(account.id), remote_uri,
                    direction,
                    remote_uri if direction == 'incoming' else str(account.id),
                    str(account.id) if direction == 'incoming' else remote_uri,
                    str(timestamp), body, content_type, "0",
                    MSG_STATE_DELIVERED if direction == 'incoming' else MSG_STATE_SENT,
                    call_id=call_id or msgid, encryption='', read=1)
                return False            # nothing was said, so nothing is unread
            payload = self._decode_location_payload(str(account.id), body, metadata, content_type)
            if payload is None:
                # A metadata flavour nothing here understands. Kept for
                # the same reason the journal keeps one: this is the only
                # copy, and a build that learns the flavour later finds
                # nothing if it was thrown away. Inert -- never unread,
                # never moves the conversation, never drawn.
                stamps_conversation_time = False
                encryption = ''
                unread = False
            else:
                body = storable_envelope(payload)
                if payload['is_coordinate']:
                    row_id = location_bubble_id(payload, msgid)
                    if payload['is_update']:
                        self.history.update_message_body(row_id, body,
                                                         merge=merge_location_bodies)
                        return False
                    msgid = row_id
                encryption = ''
                stamps_conversation_time = is_notable_action(payload)

        if direction == 'incoming':
            cpim_from, cpim_to = remote_uri, str(account.id)
            status = MSG_STATE_DELIVERED
        else:
            cpim_from, cpim_to = str(account.id), remote_uri
            status = MSG_STATE_SENT

        self.history.add_message(
            msgid, 'sms', str(account.id), remote_uri,
            direction, cpim_from, cpim_to, str(timestamp),
            body, content_type, "0", status,
            call_id=call_id or msgid, encryption=encryption,
            read=0 if (unread and direction == 'incoming') else 1,
        )
        if stamps_conversation_time:
            self.noteMessageTime(remote_uri, timestamp)
        return True

    @objc.python_method
    def _persistUnsupportedLiveMessage(self, account, remote_uri, msgid, call_id,
                                       direction, content, content_type, timestamp):
        """Keep a live message whose type nothing here understands.

        Inert, exactly like its journalled twin: already read, no unread
        count, no reordering of the contact list, and the renderer will
        not draw it. What it buys is that the information exists at all
        when a later build learns what the type means.
        """
        body = content.decode('utf-8', 'replace') if isinstance(content, bytes) else (content or '')
        if direction == 'incoming':
            cpim_from, cpim_to = remote_uri, str(account.id)
            status = MSG_STATE_DELIVERED
        else:
            cpim_from, cpim_to = str(account.id), remote_uri
            status = MSG_STATE_SENT
        self.history.add_message(
            msgid, 'sms', str(account.id), remote_uri,
            direction, cpim_from, cpim_to, str(timestamp),
            body, content_type, "0", status,
            call_id=call_id or msgid, encryption='', read=1)

    @objc.python_method
    def notificationIdentity(self, uri, fallback=None):
        """(name, icon path) to put on a notification about one address.

        The name is the contact's, not the address: a banner saying
        "enry01@sip2sip.info" is the one piece of information the user
        already has. The icon is the contact's own picture and nothing
        else -- the stand-in avatar every unknown address gets would put a
        grey silhouette on every banner, which says less than no picture
        at all.
        """
        name = str(fallback or uri or '')
        icon = None
        try:
            controller = NSApp.delegate().contactsWindowController
            contact = controller.getFirstContactFromAllContactsGroupMatchingURI(str(uri))
        except Exception:
            contact = None
        if contact is not None:
            try:
                if contact.name:
                    name = str(contact.name)
            except Exception:
                pass
            try:
                path = contact.avatar.path
                if path and os.path.isfile(path):
                    icon = path
            except Exception:
                icon = None
        return name, icon

    @objc.python_method
    def _notificationBody(self, content, content_type):
        """Short preview for the system notification. Never leaks ciphertext."""
        from MessageHost import file_transfer_summary
        body = content.decode() if isinstance(content, bytes) else (content or '')
        if body.strip().startswith('-----BEGIN PGP MESSAGE-----'):
            return NSLocalizedString("Encrypted message", "Label")
        if content_type in (LOCATION_CONTENT_TYPE, LEGACY_LOCATION_CONTENT_TYPE):
            return NSLocalizedString("Location", "Label")
        summary = file_transfer_summary(body)
        if summary is not None:
            return summary.split('\n')[0]
        body = body.strip().replace('\n', ' ')
        return body[:120] + ('...' if len(body) > 120 else '')

    @objc.python_method
    def _describe_journal_message(self, msg, direction):
        return ('journal %s id=%s content_type=%s contact=%s timestamp=%s disposition=%s'
                % (direction, msg.get('message_id'), msg.get('content_type'),
                   msg.get('contact'), msg.get('timestamp'), msg.get('disposition')))

    @objc.python_method
    def _journal_message_is_notable(self, account, msg):
        """Return True iff a journaled incoming message should bump the
        unread badge on the SMS tab.

        Mirrors what actually renders in the conversation: plain text
        always counts; for a location payload only a coordinate *origin*
        tick produces a visible bubble. Trail ticks merely refresh an
        existing bubble, the coordinate-free signals leave a system-note
        breadcrumb, and every other metadata flavour (rotation /
        consumed / label / reply / caregiver / …) and control/sync
        message is dropped without rendering — none of those should
        increment the counter.
        """
        content_type = msg['content_type']
        if content_type in ('text/plain', 'text/html') + FILE_TRANSFER_CONTENT_TYPES:
            return True
        if content_type not in (LOCATION_CONTENT_TYPE, LEGACY_LOCATION_CONTENT_TYPE):
            return False
        payload = self._decode_location_payload(
            str(account.id), msg['content'], msg.get('metadata'), content_type)
        return is_notable_action(payload)

    @objc.python_method
    def _persist_unhandled_journal_message(self, account, msg):
        """Store a journal entry whose content type nothing here handles.

        Verbatim and inert: it becomes a row with its own content type,
        already read, and it never counts towards unread or moves the
        conversation up the list. The renderer refuses to draw anything
        it does not recognise (is_renderable_content_type), so an
        unknown row costs a little disk and nothing else -- and a later
        build that learns the type finds the history intact.
        """
        direction = msg.get('direction') or 'incoming'
        contact = msg.get('contact')
        if not contact:
            return
        if direction == 'incoming':
            cpim_from, cpim_to = contact, str(account.id)
            status = MSG_STATE_DELIVERED
        else:
            cpim_from, cpim_to = str(account.id), contact
            status = MSG_STATE_SENT
        try:
            self.history.add_message(
                msg['message_id'], 'sms', str(account.id), contact,
                direction, cpim_from, cpim_to, msg['timestamp'],
                msg.get('content') or '', msg['content_type'], "0", status,
                call_id=msg['message_id'], encryption='', read=1)
        except Exception as e:
            BlinkLogger().log_error('Cannot store unhandled %s message %s: %s'
                                    % (msg.get('content_type'), msg.get('message_id'), e))

    @objc.python_method
    def _persist_journal_message(self, account, msg, direction, status, encryption,
                                 cpim_from, cpim_to, unread=False):
        """Insert (or fold) a journal message into chat_messages.

        Behaves like a pass-through to history.add_message except for
        location payloads, where it folds every trail tick into the
        share's origin row by keying on the envelope's ``sessionId`` and
        routing update ticks through history.update_message_body. Result:
        one row per share holding the latest known position, regardless
        of how many ticks the journal hands us, plus one row per
        lifecycle signal so its breadcrumb survives a reload.

        The stored body is always the v1-shaped envelope with the
        coordinates decrypted in place (see storable_envelope): Blink's
        chat_messages has no metadata column, and a row that carries its
        own envelope replays without the private key. Coordinate ticks
        whose blob we can't decrypt are dropped on the floor —
        persisting opaque ciphertext only bloats history and renders
        nothing.
        """
        body = msg['content']
        content_type = msg['content_type']
        msgid = msg['message_id']
        # Whether this moves the conversation up the contact list. A trail
        # tick is the same share still running, not a new thing said, and
        # stamping the time on every one of them kept shoving the contact
        # back to the top every few seconds for as long as someone walked.
        stamps_conversation_time = True

        # OTR wire traffic that reached the journal before the no-journal
        # header existed. The session that could open it is long gone, so
        # storing it only guarantees a wire dump in some future replay.
        if is_otr_wire_text(body):
            BlinkLogger().log_debug('Dropped journalled OTR traffic %s' % msgid)
            self._resolvePendingSave(msgid)
            return

        if content_type in FILE_TRANSFER_CONTENT_TYPES:
            self.noteFileTransferURL(account, body)

        if content_type in (LOCATION_CONTENT_TYPE, LEGACY_LOCATION_CONTENT_TYPE) \
                and reply_metadata(body) is None and peaks_metadata(body) is None:
            payload = self._decode_location_payload(
                str(account.id), body, msg.get('metadata'), content_type)
            if payload is None:
                # Not a location payload at all -- another metadata
                # flavour -- or a coordinate tick we cannot read. Nothing
                # to RENDER, which is not the same as nothing to keep:
                # the journal offers this entry once, the cursor moves
                # past it, and it is never sent again. The flavour
                # nothing understands today is exactly the one a later
                # build will want, so it is stored inert, with the body
                # it arrived with, and the renderer declines to draw it.
                stamps_conversation_time = False
                encryption = ''
            else:
                body = storable_envelope(payload)

                if payload['is_coordinate']:
                    row_id = location_bubble_id(payload, msgid)
                    if payload['is_update']:
                        # Update tick -- refresh the existing row's body so
                        # a later replay shows the most recent position. No
                        # row is inserted, so the gauge is cleared here.
                        self.history.update_message_body(row_id, body,
                                                         merge=merge_location_bodies)
                        self._resolvePendingSave(msgid)
                        return
                    # Origin tick -- persist under the bubble id so
                    # subsequent update ticks all rewrite this row.
                    msgid = row_id
                # A coordinate-free signal keeps its own message id: it is
                # a breadcrumb in the timeline, not a map, and must not
                # collide with the session row it refers to.

                # The row we store is cleartext (the coordinates were
                # opened above), so it must not claim the ciphertext lock
                # the live path never sets on these rows either.
                encryption = ''
                stamps_conversation_time = is_notable_action(payload)

        self.history.add_message(
            msgid, 'sms', str(account.id), msg['contact'],
            direction, cpim_from, cpim_to, msg['timestamp'],
            body, content_type, "0", status,
            call_id=msg['message_id'], encryption=encryption,
            read=0 if (unread and direction == 'incoming') else 1,
        )
        if stamps_conversation_time:
            self.noteMessageTime(msg['contact'], msg['timestamp'])

    @objc.python_method
    def syncIncomingMessage(self, account, msg, last_id=None):
        direction = 'incoming'
        BlinkLogger().log_debug(self._describe_journal_message(msg, direction))
        if not self._journal_bulk:
            # bulk apply has its own progress counter; tracking every save
            # here only adds dict churn and a log line per message
            self.pendingSaveMessage[msg['message_id']] = True

        if 'display' not in msg['disposition']:
            status = MSG_STATE_DISPLAYED
        else:
            status = MSG_STATE_DELIVERED

        content = msg['content'] or ''
        if content.startswith('-----BEGIN PGP MESSAGE-----') and content.endswith('-----END PGP MESSAGE-----'):
            encryption = 'pgp_encrypted'
        else:
            encryption = ''

        if not last_id:
            BlinkLogger().log_debug('journal incoming id=%s -> persist only (backfill, no last_id)' % msg['message_id'])
            unread = self._journalMessageIsUnread(account, msg, status)
            self._persist_journal_message(
                account, msg, direction, status, encryption,
                cpim_from=msg['contact'], cpim_to=str(account.id),
                unread=unread,
            )
            if unread:
                self.noteUnreadMessage(msg['contact'])
            return

        # Only open windows for messages newer than one week. For older entries
        # without an existing viewer we persist directly to history and skip the
        # GUI hop — scroll_back_in_time at the end of syncConversations will
        # surface them when the user opens the conversation.
        # A journalled message NEVER creates a conversation. Replicated
        # history is caught up into the database; the user opens a contact to
        # see it. Messages still reach a conversation that is already open.
        create_if_needed = False
        if not self._hasViewerFor(msg['contact'], account):
            BlinkLogger().log_debug('journal incoming id=%s -> persist only (no open conversation)'
                                    % msg['message_id'])
            unread = self._journalMessageIsUnread(account, msg, status)
            self._persist_journal_message(
                account, msg, direction, status, encryption,
                cpim_from=msg['contact'], cpim_to=str(account.id),
                unread=unread,
            )
            if unread:
                self.noteUnreadMessage(msg['contact'])
            return

        BlinkLogger().log_debug('journal incoming id=%s -> present in GUI (create_if_needed=%s, notable=%s)'
                                % (msg['message_id'], create_if_needed, self._journal_message_is_notable(account, msg)))
        self._presentJournalIncomingMessage(account, msg, status, create_if_needed)

    @objc.python_method
    def _conversationReadContact(self, content):
        """The contact a conversation-read marker refers to.

        Two shapes in the wild: this client sends {"contact": "..."} while
        the journal replays a bare address, the same convention
        sylk-conversation-remove uses. Accepting only the JSON one meant
        every marker arriving through replication was discarded -- which is
        precisely the path that matters, since it is another device telling
        us what it read.
        """
        text = (content or '').strip()
        if not text:
            return None
        if text.startswith('{'):
            try:
                value = json.loads(text).get('contact')
            except (TypeError, ValueError):
                return None
            return str(value).strip() if value else None
        return text if '@' in text else None

    @objc.python_method
    def applyConversationRead(self, account, content):
        """Another of my devices read this conversation, so this one has too.

        The contact is in the PAYLOAD, not in the addressing. This message
        travels from my account to my account, so the To header names me --
        going by the addressing would clear the badge on myself and leave
        the contact's untouched, which is why this never worked.
        """
        if isinstance(content, bytes):
            content = content.decode('utf-8', 'replace')
        contact = self._conversationReadContact(content)
        if not contact:
            BlinkLogger().log_error('Cannot read the conversation-read payload %r' % content)
            return

        BlinkLogger().log_info('Conversation with %s was read on another device' % contact)
        self.clearUnreadMessages(contact)

        # If this device has the conversation open, settle it there too: the
        # receipts are owed by the account, not by the device, and another
        # device has already sent them.
        for viewer in self.allViewers():
            if viewer.account == account and viewer.remote_uri == contact:
                try:
                    viewer.messages_read()
                    host = self.windowForViewer(viewer)
                    if host is not None:
                        host.noteNoMessageForSession_(viewer)
                except Exception as e:
                    BlinkLogger().log_error('Cannot settle the read conversation %s: %s'
                                            % (contact, e))

    @objc.python_method
    def _journalMessageIsUnread(self, account, msg, status):
        """Whether a journalled message is one the user has still to read.

        Catching up replicated history is still the arrival of messages the
        user has not read -- they were simply delivered while this device
        was away. Only the ones that actually render count, which is what
        _journal_message_is_notable decides; a location trail tick or a
        control message must not inflate the badge.

        Asked BEFORE the row is written, so the row can be stored unread
        and the badge can be rebuilt from the table at the next launch
        instead of living only in this process.
        """
        if status == MSG_STATE_DISPLAYED:
            return False
        try:
            return bool(self._journal_message_is_notable(account, msg))
        except Exception as e:
            BlinkLogger().log_error('Cannot tell whether journal message %s is unread: %s'
                                    % (msg.get('message_id'), e))
            return False

    @objc.python_method
    def _hasViewerFor(self, remote_uri, account):
        # allViewers() rather than a scan of self.windows: once conversations
        # can be hosted by the drawer instead of a window, a window-only scan
        # would report "no viewer" and silently divert live messages into
        # persist-only.
        for viewer in self.allViewers():
            if viewer.account == account and viewer.remote_uri == remote_uri:
                return True
        return False

    @objc.python_method
    @run_in_gui_thread
    def _presentJournalIncomingMessage(self, account, msg, status, create_if_needed):
        direction = 'incoming'
        # Per-message journal log; demoted to debug because a single sync
        # batch can deliver thousands of these and each line was being
        # fanned out to the GUI Activity panel via run_in_gui_thread.
        BlinkLogger().log_debug('Sync %s %s message %s with %s' % (msg['direction'], status, msg['message_id'], msg['contact']))

        sender_uri = SIPURI.parse(str('sip:%s' % msg['contact']))
        # History sync / replication replay must never raise the window — these
        # are past messages being caught up, not live arrivals. The window is
        # still created when recent (create_if_needed) and the unread badge is
        # set below; it just must not steal focus. note_new_message=False.
        viewer = self.getWindow(sender_uri, msg['contact'], account, create_if_needed=create_if_needed, note_new_message=False, is_replication_message=False)
        if viewer is None:
            return

        sender_identity = ChatIdentity(sender_uri, viewer.display_name)

        # create_if_needed is always False now -- a journalled message never
        # creates a conversation -- so testing it here meant the badge could
        # never fire for a journalled message at all. What matters is whether
        # the message is one the user has not seen; the host decides whether
        # the conversation is on screen and therefore already read.
        if status != MSG_STATE_DISPLAYED and self._journal_message_is_notable(account, msg):
            self.windowForViewer(viewer).noteNewMessageForSession_(viewer)

        window = self.windowForViewer(viewer).window()
        viewer.gotMessage(sender_identity, msg['message_id'], msg['message_id'], direction, (msg['content'] or '').encode(), msg['content_type'], is_replication_message=False, window=window, cpim_imdn_events=msg['disposition'], imdn_timestamp=msg['timestamp'], account=account, from_journal=True, status=status, metadata=msg.get('metadata'))

        if create_if_needed:
            self.windowForViewer(viewer).noteView_isComposing_(viewer, False)

    @objc.python_method
    def syncOutgoingMessage(self, account, msg, last_id=None):
        direction = 'outgoing'
        BlinkLogger().log_debug(self._describe_journal_message(msg, direction))

        if not self._journal_bulk:
            self.pendingSaveMessage[msg['message_id']] = True

        if msg['state'] == 'delivered':
            state = MSG_STATE_DELIVERED
        elif msg['state'] == 'displayed':
            state = MSG_STATE_DISPLAYED
        elif msg['state'] == 'failed':
            state = MSG_STATE_FAILED
        else:
            state = MSG_STATE_SENT

        content = msg['content'] or ''
        if content.startswith('-----BEGIN PGP MESSAGE-----') and content.endswith('-----END PGP MESSAGE-----'):
            encryption = 'pgp_encrypted'
        else:
            encryption = ''

        if not last_id:
            BlinkLogger().log_debug('journal outgoing id=%s -> persist only (backfill, no last_id)' % msg['message_id'])
            self._persist_journal_message(
                account, msg, direction, state, encryption,
                cpim_from=str(account.id), cpim_to=msg['contact'],
            )
            return

        # Only open windows for messages newer than one week. Older replicated
        # outgoing messages with no open viewer are persisted directly so we
        # don't pay the GUI-thread cost for every entry in the journal.
        # A journalled message NEVER creates a conversation. Replicated
        # history is caught up into the database; the user opens a contact to
        # see it. Messages still reach a conversation that is already open.
        create_if_needed = False
        if not self._hasViewerFor(msg['contact'], account):
            BlinkLogger().log_debug('journal outgoing id=%s -> persist only (no open conversation)'
                                    % msg['message_id'])
            self._persist_journal_message(
                account, msg, direction, state, encryption,
                cpim_from=str(account.id), cpim_to=msg['contact'],
            )
            return

        BlinkLogger().log_debug('journal outgoing id=%s -> present in GUI (create_if_needed=%s)'
                                % (msg['message_id'], create_if_needed))
        self._presentJournalOutgoingMessage(account, msg, state, create_if_needed, last_id)

    @objc.python_method
    @run_in_gui_thread
    def _presentJournalOutgoingMessage(self, account, msg, status, create_if_needed, last_id):
        direction = 'outgoing'
        BlinkLogger().log_debug('Sync %s %s message %s with %s' % (msg['direction'], msg['state'], msg['message_id'], msg['contact']))

        sender_identity = ChatIdentity(account.uri, account.display_name)
        remote_identity = SIPURI.parse(str('sip:%s' % msg['contact']))

        viewer = self.getWindow(remote_identity, msg['contact'], account, note_new_message=False, create_if_needed=create_if_needed)
        if viewer is None:
            return

        window = self.windowForViewer(viewer).window()

        viewer.gotMessage(sender_identity, msg['message_id'], msg['message_id'], direction, (msg['content'] or '').encode(), msg['content_type'], is_replication_message=True, window=window, cpim_imdn_events=msg['disposition'], imdn_timestamp=msg['timestamp'], account=account, from_journal=True, status=status, metadata=msg.get('metadata'))

        if (last_id and create_if_needed):
            self.windowForViewer(viewer).noteView_isComposing_(viewer, False)

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
    def useMessagePanel(self):
        """Host conversations in the main window drawer instead of the tabbed
        SMS window. See MessageHost.USE_MESSAGE_PANEL."""
        from MessageHost import USE_MESSAGE_PANEL
        return USE_MESSAGE_PANEL

    @objc.python_method
    def setMessagePanelController(self, controller):
        self._message_panel_controller = controller

    @objc.python_method
    def messagePanelController(self):
        return getattr(self, '_message_panel_controller', None)

    @objc.python_method
    def _findViewer(self, target, instance_id, account):
        for window in self.windows:
            for viewer in window.viewers:
                if viewer.matchesTargetOrInstanceAndAccount(target, instance_id, account):
                    return viewer
        for viewer, host in list(self.viewer_hosts.items()):
            if host is None or host in self.windows:
                continue
            if not self._hostHasViewer(host, viewer):
                self.viewer_hosts.pop(viewer, None)
                continue
            if viewer.matchesTargetOrInstanceAndAccount(target, instance_id, account):
                return viewer
        return None

    @objc.python_method
    def viewerForTarget(self, target, display_name, account, create_if_needed=True, instance_id=None, selected_contact=None, is_replication_message=False):
        """Find or create the SMSViewController for a conversation.

        Pure model work: never creates, raises or touches a window. Call
        presentViewer() to put the result on screen.
        """
        if instance_id and instance_id.startswith('urn:uuid:'):
            instance_id = instance_id[9:]

        if display_name and display_name.startswith("sip:"):
            display_name = display_name[4:]

        viewer = self._findViewer(target, instance_id, account)
        if viewer is None and create_if_needed:
            viewer = SMSViewController.alloc().initWithAccount_target_name_instance_(account, target, display_name, instance_id, selected_contact, is_replication_message=is_replication_message)
        return viewer

    @objc.python_method
    def _createHostForViewer(self, viewer):
        """Pick the host for a viewer that has none. The single branch point
        between the tabbed window and the drawer."""
        if self.useMessagePanel():
            panel = self.messagePanelController()
            if panel is None:
                # the pane is built lazily on first use; ask the main window
                # for it rather than silently falling back to a window
                owner = getattr(self, '_owner', None)
                if owner is not None and hasattr(owner, 'messagePane'):
                    try:
                        panel = owner.messagePane()
                    except Exception as e:
                        BlinkLogger().log_error('Cannot create the messages pane: %s' % e)
            if panel is not None:
                return panel
        if not self.windows:
            window = SMSWindowController.alloc().initWithOwner_(self)
            self.windows.append(window)
            return window
        return self.windows[0]

    @objc.python_method
    def presentViewer(self, viewer, focus=False, note_new_message=True):
        """Attach a viewer to a host if it has none, then bring it to front."""
        if viewer is None:
            return None

        host = self.windowForViewer(viewer)
        if host is None:
            host = self._createHostForViewer(viewer)
            viewer.windowController = host
            self.viewer_hosts[viewer] = host
            host.addViewer(viewer, focusTab=focus)

        if note_new_message:
            BlinkLogger().log_info("Conversation with %s presented (focus=%s)" % (viewer.target_uri, focus))
            if hasattr(host, 'bringToFront'):
                host.bringToFront(focus)
            elif focus:
                host.window().makeKeyAndOrderFront_(None)
            else:
                host.window().orderFront_(None)
            NSApp.delegate().noteNewMessage(host)

        return host

    @objc.python_method
    def getWindow(self, target, display_name, account, create_if_needed=True, note_new_message=True, focusTab=False, instance_id=None, content=None, content_type=None, selected_contact=None, is_replication_message=False):
        """Back-compat wrapper over viewerForTarget + presentViewer.

        Returns the viewer, as it always has, despite the name.
        """
        viewer = self.viewerForTarget(target, display_name, account, create_if_needed=False, instance_id=instance_id, selected_contact=selected_contact, is_replication_message=is_replication_message)

        if content_type == IMDNDocument.content_type:
            # IMDN never creates or raises anything; it only moves statuses.
            if not viewer:
                #BlinkLogger().log_error('No viewer found')
                return

            try:
                document = IMDNDocument.parse(content)
            except ParserError as e:
                BlinkLogger().log_error('Failed to parse IMDN payload: %s' % str(e))
            else:
                imdn_message_id = document.message_id.value
                imdn_status = document.notification.status.__str__()

                if imdn_status == 'delivered':
                    viewer.update_message_status(imdn_message_id, MSG_STATE_DELIVERED)
                elif imdn_status == 'displayed':
                    viewer.update_message_status(imdn_message_id, MSG_STATE_DISPLAYED)
                elif imdn_status == 'failed':
                    viewer.update_message_status(imdn_message_id, MSG_STATE_FAILED)

        if viewer is None and create_if_needed:
            viewer = self.viewerForTarget(target, display_name, account, create_if_needed=True, instance_id=instance_id, selected_contact=selected_contact, is_replication_message=is_replication_message)

        if viewer is not None:
            self.presentViewer(viewer, focus=focusTab, note_new_message=note_new_message)

        return viewer

    @objc.python_method
    def dettachSMSViewer(self, viewer):
        oldWindow = self.windowForViewer(viewer)
        oldWindow.removeViewer_(viewer)
        window = SMSWindowController.alloc().initWithOwner_(self)
        self.windows.append(window)
        window.addViewer(viewer)
        self.viewer_hosts[viewer] = window
        window.window().makeKeyAndOrderFront_(None)
        return window

    @objc.python_method
    def windowForViewer(self, viewer):
        """The host currently showing this viewer, or None.

        Named for its history; hostForViewer() is the same thing under the
        name the drawer work uses.
        """
        host = self.viewer_hosts.get(viewer)
        if host is not None and self._hostHasViewer(host, viewer):
            return host

        for window in self.windows:
            if viewer in window.viewers:
                self.viewer_hosts[viewer] = window
                return window

        self.viewer_hosts.pop(viewer, None)
        return None

    @objc.python_method
    def hostForViewer(self, viewer):
        return self.windowForViewer(viewer)

    @objc.python_method
    def _hostHasViewer(self, host, viewer):
        try:
            return viewer in host.viewers
        except Exception:
            return False

    @objc.python_method
    def handle_notification(self, notification):
        # MessageSaved fires once per history insert -- thousands of them
        # during a journal sync. It only touches a dict and the log, so
        # paying a GUI-thread dispatch for each was enough on its own to
        # freeze the app for the length of the sync.
        if notification.name == 'MessageSaved':
            self._NH_MessageSaved(notification.sender, notification.data)
            return
        self._handleNotificationOnGUIThread(notification)

    @objc.python_method
    @run_in_gui_thread
    def _handleNotificationOnGUIThread(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    @objc.python_method
    def escrowQuestionAnswered(self, account):
        """Whether we yet know if the server holds a key for this account.

        Generating a keypair is irreversible in the only way that matters --
        the messages encrypted to the old one stop being readable -- so the
        offer waits until the addressbook has been seen. The wait is bounded:
        an account with no XCAP has nothing to wait for, and after 15 seconds
        an unanswered fetch stops being a reason to withhold the prompt.
        """
        if account.id in self.key_escrow_checked:
            return True
        if not account.xcap.enabled:
            return True
        first_asked = self.generate_prompt_deferred.setdefault(account.id, time.time())
        return time.time() - first_asked > 15

    @objc.python_method
    @run_in_gui_thread
    def escrowPrivateKey(self, account, force=False):
        """Publish this device's keypair onto our own XCAP contact.

        The escrow lets another of the user's devices adopt this account's
        existing key instead of generating a fresh one and orphaning every
        message encrypted to the old one. KeyEscrow refuses on its own when
        writing would destroy a key rather than back one up; this only reports
        what it decided.
        """
        if account is None or account is BonjourAccount():
            return

        written, reason = write_self_keys(account, force=force)

        alert = NSAlert.alloc().init()
        alert.setMessageText_(NSLocalizedString("PGP Key Escrow", "Window title"))
        if written:
            alert.setInformativeText_(NSLocalizedString("The private key of account %s has been encrypted with the account password and saved on the server, on %d contact(s). Your other devices can now adopt this key instead of generating their own.", "label") % (account.id, len(written)))
        else:
            alert.setInformativeText_(NSLocalizedString("The key of account %s was not saved. %s", "label") % (account.id, reason or ''))
        alert.addButtonWithTitle_(NSLocalizedString("OK", "button"))
        alert.runModal()

    @objc.python_method
    @run_in_gui_thread
    def showExportPrivateKeyPanel(self, account):
        self.export_key_window = ExportPrivateKeyController(account, self.sendMessage);

    @objc.python_method
    @run_in_gui_thread
    def showGeneratePrivateKeyPanel(self, account):
        # Private keys are never generated silently. Show a confirmation
        # modal first; if a key already exists, warn that it will be
        # overwritten and old encrypted messages will become unreadable.
        if account is None or account is BonjourAccount():
            return False

        if not self.escrowQuestionAnswered(account):
            # The addressbook has not come back yet, and it may be carrying
            # this account's real key. Generating one now would mint a second
            # keypair and leave every existing message unreadable, so say
            # nothing; the prompt comes back the next time a viewer opens.
            BlinkLogger().log_info('Waiting for the addressbook of %s before offering to generate a '
                                   'PGP key -- it may already carry one' % account.id)
            return False

        keys_path = ApplicationData.get('keys')
        default_path = "%s/%s.privkey" % (keys_path, account.id)
        has_key = (bool(account.sms.private_key) and os.path.exists(account.sms.private_key)) or os.path.exists(default_path)

        alert = NSAlert.alloc().init()
        alert.setMessageText_(NSLocalizedString("Generate PGP Private Key", "Window title"))
        if has_key:
            alert.setInformativeText_(NSLocalizedString("A PGP private key already exists for account %s. Generating a new key will permanently overwrite it, and any messages encrypted with the old key will no longer be readable. Do you want to continue?", "label") % account.id)
        else:
            alert.setInformativeText_(NSLocalizedString("Account %s has no PGP private key yet. A key is required to send and receive encrypted messages. Do you want to generate one now?", "label") % account.id)
        alert.addButtonWithTitle_(NSLocalizedString("Generate", "button"))
        alert.addButtonWithTitle_(NSLocalizedString("Cancel", "button"))

        if alert.runModal() != NSAlertFirstButtonReturn:
            BlinkLogger().log_info("PGP key generation cancelled for %s" % account.id)
            return False

        try:
            generate_pgp_keypair(account)
        except Exception as e:
            BlinkLogger().log_error("PGP key generation failed: %s" % str(e))
            error = NSAlert.alloc().init()
            error.setMessageText_(NSLocalizedString("Key Generation Failed", "Window title"))
            error.setInformativeText_(str(e))
            error.addButtonWithTitle_(NSLocalizedString("OK", "button"))
            error.runModal()
            return False

        BlinkLogger().log_info("PGP key generated for %s" % account.id)
        return True

    @objc.python_method
    @objc.python_method
    def _log_incoming_message(self, direction, account, sender_identity, recipient,
                              content_type, content, imdn_id, is_cpim,
                              replicated, instance_id):
        """One line per message off the wire, logged before anything decides
        what to do with it.

        Everything past this point either dispatches on the content type or
        drops the message, and a dropped one used to leave no trace at all --
        which is why an unsupported type, or a payload filed under the wrong
        contact, could only be found by guessing. This is the raw facts in
        the order they matter: who, to whom, what, and how much of it.
        """
        try:
            size = len(content) if content is not None else 0
        except TypeError:
            size = 0

        head = ''
        try:
            if isinstance(content, bytes):
                head = content[:64].decode('utf-8', 'replace')
            elif isinstance(content, str):
                head = content[:64]
        except Exception:
            head = ''

        try:
            origin = format_identity_to_string(sender_identity)
        except Exception:
            origin = str(sender_identity)

        parts = ['direction=%s' % direction,
                 'from=%s' % origin,
                 'to=%s' % recipient,
                 'account=%s' % (account.id if account is not None else 'none'),
                 'type=%s' % (content_type or 'none'),
                 'size=%d' % size,
                 'id=%s' % (imdn_id or '-')]
        if is_cpim:
            parts.append('cpim')
        if replicated:
            parts.append('replicated')
        if 'BEGIN PGP MESSAGE' in head:
            parts.append('encrypted')
        if instance_id:
            parts.append('instance=%s' % instance_id)

        BlinkLogger().log_info('Message received: %s' % ' '.join(parts))


    @objc.python_method
    def _dump_file_transfer_payload(self, content, metadata=None):
        """The whole envelope of a file transfer that arrived LIVE.

        Only the live path, deliberately. A journalled copy has already
        been through the server's store and replay, so it cannot answer
        the question this exists for: whether a field the sender says it
        sent -- a recording's peaks, its spectrum, its duration -- was on
        the wire in the first place, or was lost somewhere after. Dumping
        the replayed copy too would just produce two versions of the same
        ambiguity.

        Logged whole rather than summarised: a summary can only report
        the fields somebody thought to look for, and the point of this is
        the field nobody expected to be missing.
        """
        try:
            body = content.decode('utf-8', 'replace') if isinstance(content, bytes) else content
        except Exception as e:
            BlinkLogger().log_error('Cannot read the file transfer payload: %s' % e)
            return
        if not isinstance(body, str):
            return

        BlinkLogger().log_info('File transfer payload (live, %d bytes):' % len(body))
        try:
            envelope = json.loads(body)
        except (TypeError, ValueError):
            # Not JSON: an RCS XML transfer, or an encrypted body. Say so
            # and print it -- unreadable here is itself the answer.
            BlinkLogger().log_info('  not JSON: %s' % body[:800])
        else:
            if isinstance(envelope, dict):
                BlinkLogger().log_info('  keys: %s' % ','.join(sorted(envelope)))
                for key in sorted(envelope):
                    BlinkLogger().log_info('  %s = %s'
                                           % (key, _describe_payload_value(envelope[key])))
            else:
                BlinkLogger().log_info('  %s' % body[:800])

        if metadata:
            # The CPIM side-band. Location v2 travels here; if a recording
            # ever ships its peaks this way instead of in the body, this is
            # where they would show up -- and Blink drops it for file
            # transfers, which would be the bug.
            text = metadata if isinstance(metadata, str) else str(metadata)
            BlinkLogger().log_info('  CPIM Metadata side-band (%d bytes): %s'
                                   % (len(text), text[:600]))

    @objc.python_method
    def _seenMessage(self, key):
        """Whether this id has been taken in before. Records it if not.

        One ring for both kinds of id: they are drawn from different
        headers but they answer the same question, and a message whose
        Call-Id is new while its Message-ID is not is still the message
        the user has already read.
        """
        if not key:
            return False
        seen = self.seen_message_ids
        if key in seen:
            seen.move_to_end(key)
            return True
        seen[key] = True
        while len(seen) > self.MAX_SEEN_MESSAGE_IDS:
            seen.popitem(last=False)
        return False

    def _NH_SIPEngineGotMessage(self, sender, data):
        is_cpim = False
        cpim_message = None
        imdn_id = str(uuid.uuid4())
        imdn_timestamp = None
        cpim_imdn_events = None
        # The generic per-message side-band: a CPIM *body* header named
        # Metadata in the urn:ag-projects:xml:ns:cpim namespace (prefix
        # agp). Location sharing is its first user — from payload version
        # 2 on, the cleartext lifecycle envelope travels here and the
        # body is nothing but the encrypted coordinates.
        metadata = None
    
        call_id = data.headers.get('Call-ID', Null).body
        is_replication_message = data.headers.get('X-Replicated-Message', Null).body
        instance_id = data.from_header.uri.parameters.get('instance_id', None)
        # The transport's own repeat: same request, same Call-Id, sent
        # again because our answer did not get back in time.
        if self._seenMessage(call_id):
            BlinkLogger().log_info('Dropped a repeat of the message with Call-Id %s' % call_id)
            return
            
        direction = 'incoming'

        try:
            data.request_uri.parameters['instance_id']
        except KeyError:
            if is_replication_message:
                account = AccountManager().find_account(data.from_header.uri)

                if account and not account.enabled:
                    account = None

                if not account:
                    direction = 'incoming'
                    account = AccountManager().find_account(data.to_header.uri)
                    if account and not account.enabled:
                        account = None
                else:
                    direction = 'outgoing'

                if not account:
                    BlinkLogger().log_warning("Could not find local enabled account for message from %s to %s" % (data.from_header.uri, data.to_header.uri))
                    return
            else:
                account = AccountManager().find_account(data.to_header.uri)
        else:
            account = BonjourAccount()

        if data.content_type == 'message/cpim':
            is_cpim = True
            imdn_id = None

            try:
                cpim_message = CPIMPayload.decode(data.body)
            except CPIMParserError:
                BlinkLogger().log_warning("Incoming message from %s has invalid CPIM content" % format_identity_to_string(data.from_header))
                return
            else:
                content = cpim_message.content
                content_type = cpim_message.content_type
                imdn_timestamp = cpim_message.timestamp

                for h in cpim_message.additional_headers:
                    if h.name == "Message-ID":
                        imdn_id = h.value
                    if h.name == "Disposition-Notification":
                        cpim_imdn_events = h.value
                    if h.name == "Metadata":
                        metadata = h.value
                
                sender_identity = cpim_message.sender or data.from_header
                if direction == 'outgoing':
                    window_tab_identity = cpim_message.recipients[0] if cpim_message.recipients else data.to_header
                else:
                    window_tab_identity = sender_identity

        else:
            content = data.body
            content_type = data.content_type
            sender_identity = data.from_header
            window_tab_identity = data.to_header if direction == 'outgoing' else sender_identity

        # Every message needs an id that a second copy of it would share.
        # A CPIM body with no Message-ID header left this as None, and None
        # is not an identity: two copies of such a message looked like two
        # different messages to every check downstream, the renderer's
        # included. The Call-Id is the next best thing -- it is at least
        # this message's own.
        if is_cpim and not imdn_id:
            imdn_id = call_id

        # The sender's own repeat: a new transaction carrying a message we
        # have already taken in, sent because our receipt never got back.
        # Only the CPIM Message-ID can tell us that -- it is the one id the
        # two copies share, the Call-Id having changed with the transaction.
        #
        # Is-composing is exempt. It is a state rather than a message, and a
        # sender that reuses one id for the whole time it is typing would go
        # quiet after the first indication. IMDN receipts are exempt for the
        # opposite reason: a client is free to send its receipt under the id
        # of the message it is reporting on, and dropping one as a repeat of
        # that message would cost a delivered or read mark for ever. Acting
        # on the same receipt twice costs nothing -- it sets a status that
        # is already set.
        if (is_cpim and imdn_id and imdn_id != call_id
                and content_type not in (IsComposingDocument.content_type,
                                         IMDNDocument.content_type)
                and self._seenMessage(imdn_id)):
            BlinkLogger().log_info('Dropped a second copy of message %s from %s'
                                   % (imdn_id, format_identity_to_string(sender_identity)))
            return

        uri = format_identity_to_string(window_tab_identity)
        self._log_incoming_message(direction, account, sender_identity, uri,
                                   content_type, content, imdn_id, is_cpim,
                                   is_replication_message, instance_id)

        # Live only: a replicated copy has already been through the
        # server's store and replay, so it cannot say what was on the wire.
        if content_type in FILE_TRANSFER_CONTENT_TYPES and not is_replication_message:
            self._dump_file_transfer_payload(content, metadata)
        
        if content_type == 'text/pgp-public-key':
            BlinkLogger().log_info('Public key message received from %s for %s (%s)'
                                   % (format_identity_to_string(sender_identity),
                                      uri, 'our own, replicated' if direction == 'outgoing'
                                      else 'incoming'))
            # An outgoing key is OUR key, replicated back to us because we
            # sent it from another device. `uri` here is the recipient, so
            # importing it would file our own key under their address and
            # destroy the key we actually hold for them. There is nothing to
            # import either way: we already have our own key.
            if direction == 'outgoing':
                BlinkLogger().log_info('Our own public key was replicated to %s, nothing to import' % uri)
                return

            #BlinkLogger().log_info(u"Public key of %s received" % (format_identity_to_string(sender_identity)))
            # Public-key exchange is a control message — never raise a window.
            viewer = self.getWindow(SIPURI.new(window_tab_identity.uri), window_tab_identity.display_name, account, instance_id=instance_id, create_if_needed=False, note_new_message=False, content=content, content_type=content_type)
           
            try:
                acc = AccountManager().get_account(uri);
            except KeyError:
                pass
            else:
                if acc.sms.private_key:
                    BlinkLogger().log_info(u"Public key save skipped for accounts that have private keys")
                    return

            public_key = ''
            start_public = False

            for l in content.decode().split("\n"):
                if l == "-----BEGIN PGP PUBLIC KEY BLOCK-----":
                    start_public = True

                if l == "-----END PGP PUBLIC KEY BLOCK-----":
                    public_key = public_key + l + '\n'
                    start_public = False
                    break

                if start_public:
                    public_key = public_key + l + '\n'
            
            if public_key:
                self._warn_if_key_mismatched(public_key, uri)
                public_key_checksum = hashlib.sha1(public_key.encode()).hexdigest()
                key_file = "%s/%s.pubkey" % (self.keys_path, uri)
                fd = open(key_file, "wb+")
                fd.write(public_key.encode())
                fd.close()
                nc_title = NSLocalizedString("Public key", "System notification title")
                nc_subtitle = format_identity_to_string(sender_identity, check_contact=True, format='full')
                nc_body = NSLocalizedString("Public key received", "System notification title")
                #NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)
                self.notification_center.post_notification('PGPPublicKeyReceived', sender=account, data=NotificationData(uri=uri, key=public_key))
                
                self.saveContact(uri, {'public_key': key_file, 'public_key_checksum': public_key_checksum})
                #BlinkLogger().log_info(u"Public key for %s saved to %s" % (uri, key_file))
            else:
                 BlinkLogger().log_info(u"No public PGP key detected in the payload")
            return

        elif content_type == 'application/sylk-contact-update':
            self.contacts_queue.put({'account': account.id, 'data': content.decode()})
            return
        elif content_type == 'text/pgp-private-key':
            BlinkLogger().log_info('PGP private key from %s to %s received' % (data.from_header.uri, account.id))

            if account.id == str(data.from_header.uri).split(":")[1]:
                public_key = ''
                private_key_encrypted = ''

                start_public = False
                start_private = False

                for l in content.decode().split("\n"):
                    if l == "-----BEGIN PGP PUBLIC KEY BLOCK-----":
                        start_public = True

                    if l == "-----BEGIN PGP MESSAGE-----":
                        start_public = False
                        start_private = True

                    if start_public:
                        public_key = public_key + l + "\n"

                    if start_private:
                        private_key_encrypted = private_key_encrypted + l + "\n"

                public_key_path = "%s/%s.pubkey" % (self.keys_path, account.id)

                try:
                    _public_key = open(public_key_path, 'rb').read()
                except Exception as e:
                    BlinkLogger().log_info('Cannot import my own PGP public key: %s' % str(e))
                else:
                    if _public_key.decode().strip() == public_key.strip():
                        BlinkLogger().log_info('PGP keys are the same')
                        nc_title = NSLocalizedString("Private key", "System notification title")
                        nc_subtitle = format_identity_to_string(account, format='full')
                        nc_body = NSLocalizedString("Private key is the same", "System notification title")
                        NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)
                        return
                    else:
                        BlinkLogger().log_info('PGP keys differ')

                if not private_key_encrypted:
                    self.log_info('PGP private key not found')
                    return

                self.import_key_window = ImportPrivateKeyController(account, public_key, private_key_encrypted);
            return
        elif content_type == 'application/sylk-api-token':
            BlinkLogger().log_info('Sylk history token for %s received' % account.id)
            try:
                data = json.loads(content)
            except (TypeError, json.decoder.JSONDecodeError):
                pass
            else:
                try:
                    token = data['token']
                    url = data['url']
                except KeyError:
                    BlinkLogger().log_info('Failed to parse history url payload %s' % data)
                else:
                    account.sms.history_token = token
                    account.sms.history_url = url
                    account.save()
                    self.syncConversations(account)
                    BlinkLogger().log_info('Saved history url %s' % url)

            return
        elif content_type == IsComposingDocument.content_type:
            content = cpim_message.content if is_cpim else data.body
            try:
                msg = IsComposingMessage.parse(content)
            except ParserError as e:
                BlinkLogger().log_error('Failed to parse Is-Composing payload: %s' % str(e))
            else:
                state = msg.state.value
                refresh = msg.refresh.value if msg.refresh is not None else None
                content_type = msg.content_type.value if msg.content_type is not None else None
                last_active = msg.last_active.value if msg.last_active is not None else None

                viewer = self.getWindow(SIPURI.new(window_tab_identity.uri), window_tab_identity.display_name, account, create_if_needed=False, note_new_message=False, instance_id=instance_id)

                if viewer:
                    viewer.gotIsComposing(self.windowForViewer(viewer), state, refresh, last_active)
            return
        elif content_type == IMDNDocument.content_type:
            # Delivery/display receipts must update message state silently —
            # they must never create or raise a window.
            viewer = self.getWindow(SIPURI.new(window_tab_identity.uri), window_tab_identity.display_name, account, instance_id=instance_id, create_if_needed=False, note_new_message=False, content=content, content_type=content_type)
            return

        elif content_type == 'application/sylk-conversation-read':
            self.applyConversationRead(account, content)
            return

        elif content_type == 'application/sylk-conversation-remove':
           self.history.delete_messages(local_uri=str(account.id), remote_uri=msg['content'])
           self.history.delete_messages(local_uri=msg['content'], remote_uri=str(account.id))
           return

        elif content_type not in ('text/plain', 'text/html', 'application/sylk-message-remove',
                                  LOCATION_CONTENT_TYPE, LEGACY_LOCATION_CONTENT_TYPE
                                  ) + FILE_TRANSFER_CONTENT_TYPES:
            # Nothing here renders this, so it is stored rather than
            # dropped. The live copy and the journalled one are the same
            # message; refusing the live one only means waiting for a
            # catch-up to bring it back, and if the catch-up has already
            # passed this point it never will.
            BlinkLogger().log_warning('Message type %s is not supported; storing it unread'
                                      % content_type)
            try:
                self._persistUnsupportedLiveMessage(
                    account, format_identity_to_string(window_tab_identity, format='aor'),
                    imdn_id, call_id, direction, content, content_type,
                    imdn_timestamp or ISOTimestamp.now())
            except Exception as e:
                BlinkLogger().log_error('Cannot store %s message %s: %s'
                                        % (content_type, imdn_id, e))
            return

        note_new_message = content_type in ('text/plain', 'text/html',
                                            LOCATION_CONTENT_TYPE, LEGACY_LOCATION_CONTENT_TYPE
                                            ) + FILE_TRANSFER_CONTENT_TYPES and direction == 'incoming'
        # Only genuine incoming chat text (text/* — text/plain, text/html)
        # may raise the window to the front. Location payloads (coordinate
        # ticks and lifecycle signals alike) and every control/sync
        # message must update silently: they must never pop the window or
        # steal focus.
        # Messages sent by myself (any of my own accounts, e.g. from another
        # device) must not raise the window.
        sender_uri = format_identity_to_string(sender_identity)
        is_myself = sender_uri == str(account.id) or AccountManager().has_account(sender_uri)
        raise_window = content_type.startswith('text/') and direction == 'incoming' and not is_myself
        # A live share emits one origin tick followed by many map-update
        # ticks and, at the end, a coordinate-free teardown signal. Only
        # the origin opens a bubble, so only the origin may bump the
        # unread counter.
        if (note_new_message
                and content_type in (LOCATION_CONTENT_TYPE, LEGACY_LOCATION_CONTENT_TYPE)
                and self._is_silent_location_tick(str(account.id), content, metadata, content_type)):
            note_new_message = False
        # NOTHING arriving over the wire creates a conversation. An open
        # conversation still receives the message live; otherwise the message
        # is written straight to history, the contact's unread count goes up
        # and a notification is posted. The user opens the contact to read it.
        viewer = self.getWindow(SIPURI.new(window_tab_identity.uri), window_tab_identity.display_name, account, note_new_message=raise_window, create_if_needed=False, instance_id=instance_id)

        if content_type == 'application/sylk-message-remove':
            try:
                json_data = json.loads(content.decode())
                msg_id = json_data['message_id']
            except (json.decoder.JSONDecodeError, TypeError, KeyError) as e:
                BlinkLogger().log_debug('Error parsing message remove %s: %s' % (content.decode(), str(e)))
            else:
                if viewer:
                    viewer.delete_message(msg_id, local=True)
                else:
                    # No open conversation to update — delete from history directly.
                    self.history.delete_message(msg_id)

            return

        if viewer is None:
            # No conversation open for this contact: persist and notify.
            remote_uri = format_identity_to_string(window_tab_identity, format='aor')
            try:
                stored = self._persistLiveMessage(
                    account, remote_uri, imdn_id, call_id, direction,
                    content, content_type, imdn_timestamp or ISOTimestamp.now(),
                    metadata=metadata, unread=note_new_message)
            except Exception as e:
                BlinkLogger().log_error('Cannot store live message %s from %s: %s'
                                        % (imdn_id, remote_uri, e))
                return

            if stored and note_new_message:
                # A live message with no conversation open has the same
                # problem replicated ones did: nothing else files the sender
                # under Messages, so an unread badge would have no row to sit
                # on.
                self.ensureMessagesGroupContains(remote_uri)
                self.addContactsToMessagesGroup()
                count = self.noteUnreadMessage(remote_uri)
                BlinkLogger().log_info('Message %s from %s stored, %d unread'
                                       % (content_type, remote_uri, count))
                # The sender is the title, the message is the body: macOS
                # already puts "Blink" above both, so a title of "New
                # message" spends the most prominent line saying what the
                # banner obviously is.
                name, icon = self.notificationIdentity(
                    remote_uri, window_tab_identity.display_name)
                NSApp.delegate().notify_new_message(
                    name, self._notificationBody(content, content_type),
                    None, uri=remote_uri, icon=icon)
            return

        if note_new_message:
            self.windowForViewer(viewer).noteNewMessageForSession_(viewer)

        status = MSG_STATE_DELIVERED if direction == 'incoming' else MSG_STATE_SENT

        window = self.windowForViewer(viewer).window()
        viewer.gotMessage(sender_identity, imdn_id, call_id, direction, content, content_type, is_replication_message=is_replication_message, window=window, cpim_imdn_events=cpim_imdn_events, imdn_timestamp=imdn_timestamp, account=account, status=status, metadata=metadata)
        
        self.windowForViewer(viewer).noteView_isComposing_(viewer, False)


class ImportPrivateKeyController(NSObject):
    window = objc.IBOutlet()
    pincode = objc.IBOutlet()
    status = objc.IBOutlet()
    importButton = objc.IBOutlet()
    publicKey = None
    privateKey = None
    dealloc_timer = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, account, public_key, private_key_encrypted):
        NSBundle.loadNibNamed_owner_("ImportPrivateKeyWindow", self)
        self.keys_path = ApplicationData.get('keys')
        makedirs(self.keys_path)

        self.account = account;
        self.private_key_encrypted = private_key_encrypted
        self.public_key = public_key
        self.importButton.setEnabled_(False)
        self.window.makeFirstResponder_(self.pincode)
        self.status.setTextColor_(NSColor.blackColor())
        self.status.setStringValue_(NSLocalizedString("Enter pincode to decrypt the key", "status label"));
        self.window.makeKeyAndOrderFront_(None)

    @objc.python_method
    def update(self, account, public_key, private_key_encrypted):
        self.account = account
        self.public_key = public_key
        self.private_key_encrypted = private_key_encrypted
        self.importButton.setEnabled_(False)
        self.window.makeFirstResponder_(self.pincode)

    @objc.IBAction
    def importButtonClicked_(self, sender):
        pincode = str(self.pincode.stringValue()).strip()
        BlinkLogger().log_info("Importing private key...")

        try:
            pgpMessage = pgpy.PGPMessage.from_blob(self.private_key_encrypted.encode())
            decryptedKeyPair = pgpMessage.decrypt(pincode)
            private_key = decryptedKeyPair.message

            BlinkLogger().log_info("Private decrypted")

            self.importButton.setEnabled_(False)
            BlinkLogger().log_info("Key imported sucessfully")
            
            # Same installer the escrow restore uses, so both write the same
            # files and set the same settings -- and so a key being replaced
            # here is archived rather than overwritten.
            install_keypair(self.account, private_key, self.public_key)

            if self.dealloc_timer is None:
                self.dealloc_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(6.0, self, "deallocTimer:", None, True)
                NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSRunLoopCommonModes)
                NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSEventTrackingRunLoopMode)

#        except (pgpy.errors.PGPDecryptionError, pgpy.errors.PGPError) as e:
        except Exception as e:
            BlinkLogger().log_error("Import private key failed: %s" % str(e))
            self.status.setStringValue_(NSLocalizedString("Key import failed: %s", "status label") % str(e));
            self.status.setTextColor_(NSColor.redColor())
            import traceback
            traceback.print_exc()
        else:
            self.status.setTextColor_(NSColor.greenColor())
            self.status.setStringValue_(NSLocalizedString("Key imported sucessfully", "status label"));

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

    def windowWillClose_(self, notification):
        pass

    def close(self):
        self.window.close()

    def dealloc(self):
        #print('Dealloc ImportPrivateKeyController')
        objc.super(ImportPrivateKeyController, self).dealloc()


class ExportPrivateKeyController(NSObject):
    window = objc.IBOutlet()
    pincode = objc.IBOutlet()
    status = objc.IBOutlet()
    exportButton = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, account, sendMessageFunc):
        NSBundle.loadNibNamed_owner_("ExportPrivateKeyWindow", self)
        self.keys_path = ApplicationData.get('keys')
        makedirs(self.keys_path)

        self.account = account;
        self.sendMessage = sendMessageFunc
        self.passcode = ''.join([str(random.randint(0, 999)).zfill(3) for _ in range(2)])
        self.pincode.setStringValue_(self.passcode);
        self.status.setStringValue_(self.account.id);
        self.window.makeKeyAndOrderFront_(None)

    @objc.IBAction
    def exportButtonClicked_(self, sender):
        #BlinkLogger().log_info("Exporting private key...")

        try:
            self.exportButton.setEnabled_(False)
            private_key_path = "%s/%s.privkey" % (self.keys_path, self.account.id)
            private_key = open(private_key_path, 'rb').read()
            public_key_path = "%s/%s.pubkey" % (self.keys_path, self.account.id)
            public_key = open(public_key_path, 'rb').read()

            pgpMessage = pgpy.PGPMessage.new(private_key)
            enc_message = pgpMessage.encrypt(self.passcode)
            message = public_key.decode() + str(enc_message)

            self.sendMessage(self.account, message, 'text/pgp-private-key')

        except Exception as e:
            BlinkLogger().log_error("Export private key failed: %s" % str(e))
            self.status.setStringValue_(NSLocalizedString("Export failed: %s", "status label") % str(e));
            self.status.setTextColor_(NSColor.redColor())
            import traceback
            traceback.print_exc()
        else:
            self.status.setTextColor_(NSColor.blueColor())
            self.status.setStringValue_(NSLocalizedString("Key Exported sucessfully", "status label"));
            BlinkLogger().log_info("Key exported sucessfully")

    @objc.IBAction
    def cancelButtonClicked_(self, sender):
        self.close()

    def close(self):
        self.window.close()

    def windowWillClose_(self, notification):
        pass

    def dealloc(self):
        print('Dealloc ExportPrivateKeyController')
        objc.super(ExportPrivateKeyController, self).dealloc()
