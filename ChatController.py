# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSCompositeSourceOver,
                    NSDocumentTypeDocumentAttribute,
                    NSExcludedElementsDocumentAttribute,
                    NSEventTrackingRunLoopMode,
                    NSFontAttributeName,
                    NSHTMLTextDocumentType,
                    NSImageCompressionFactor,
                    NSInformationalRequest,
                    NSJPEGFileType,
                    NSPNGFileType,
                    NSOffState,
                    NSUTF8StringEncoding,
                    NSString,
                    NSSplitViewDidResizeSubviewsNotification,
                    NSSplitViewDividerStyleThick,
                    NSSplitViewDividerStyleThin,
                    NSToolbarPrintItemIdentifier,
                    NSWindowBelow)

from Foundation import (NSAttributedString,
                        NSBitmapImageRep,
                        NSBundle,
                        NSColor,
                        NSData,
                        NSDate,
                        NSDictionary,
                        NSFont,
                        NSImage,
                        NSMakeRect,
                        NSMakeSize,
                        NSMakeRange,
                        NSMenuItem,
                        NSNotificationCenter,
                        NSObject,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSScreen,
                        NSLocalizedString,
                        NSTask,
                        NSTaskDidTerminateNotification,
                        NSTimer,
                        NSUserDefaults,
                        NSZeroSize,
                        NSURL,
                        NSWorkspace,
                        NSDownloadsDirectory,
                        NSSearchPathForDirectoriesInDomains,
                        NSUserDomainMask
                        )

from Quartz import (CGDisplayBounds,
                    CGImageGetWidth,
                    CGMainDisplayID,
                    CGWindowListCopyWindowInfo,
                    CGWindowListCreateImage,
                    kCGWindowImageBoundsIgnoreFraming,
                    kCGWindowListExcludeDesktopElements,
                    kCGWindowListOptionIncludingWindow,
                    kCGWindowNumber)
import base64
import datetime
import hashlib
import os
import objc
import re
import random
import string
import time
import unicodedata
import uuid
import traceback

from otr import OTRState
from util import call_later

from dateutil.tz import tzlocal
from gnutls.errors import GNUTLSError

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.system import host
from itertools import chain
from zope.interface import implementer

from sipsimple.account import BonjourAccount
from sipsimple.core import SDPAttribute, SIPURI
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams.msrp.chat import ChatStream, ChatStreamError, ChatIdentity, SMPStatus
from sipsimple.threading.green import run_in_green_thread
from sipsimple.application import SIPApplication
from sipsimple.util import ISOTimestamp

import ChatWindowController
from BlinkLogger import BlinkLogger
from ChatViewController import ChatViewController, MSG_STATE_FAILED, MSG_STATE_SENDING, MSG_STATE_DELIVERED
from ChatOTR import ChatOtrSmp
from ContactListModel import BlinkPresenceContact
from ContactListModel import encode_icon, decode_icon
from FileTransferWindowController import openFileTransferSelectionDialog
from HistoryManager import ChatHistory
from MediaStream import MediaStream, STATE_IDLE, STREAM_IDLE, STREAM_FAILED, STREAM_CONNECTED, STREAM_PROPOSING, STREAM_WAITING_DNS_LOOKUP, STREAM_INCOMING, STREAM_CONNECTING, STREAM_RINGING, STREAM_DISCONNECTING, STREAM_CANCELLING
from MediaStream import STATE_IDLE
from PhotoPicker import PhotoPicker
from SIPManager import SIPManager
from SmileyManager import SmileyManager
from ScreensharingPreviewPanel import ScreensharingPreviewPanel
from resources import ApplicationData
from util import allocate_autorelease_pool, format_identity_to_string, format_size, html2txt, image_file_extension_pattern, sipuri_components_from_string, run_in_gui_thread


# Copied from Carbon.h
kUIModeNormal = 0
kUIModeContentSuppressed = 1
kUIModeContentHidden = 2
kUIModeAllSuppressed = 4
kUIModeAllHidden = 3
kUIOptionAutoShowMenuBar = 1 << 0
kUIOptionDisableAppleMenu = 1 << 2
kUIOptionDisableProcessSwitch = 1 << 3
kUIOptionDisableForceQuit = 1 << 4
kUIOptionDisableSessionTerminate = 1 << 5
kUIOptionDisableHide = 1 << 6

MAX_MESSAGE_LENGTH = 16*1024

TOOLBAR_SCREENSHARING_MENU_REQUEST_REMOTE = 201
TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL = 202
TOOLBAR_SCREENSHARING_MENU_CANCEL = 203

TOOLBAR_SCREENSHOT_MENU_WINDOW = 301
TOOLBAR_SCREENSHOT_MENU_AREA = 302

TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_HIGH = 401
TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_LOW = 402
TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_MEDIUM = 403

bundle = NSBundle.bundleWithPath_('/System/Library/Frameworks/Carbon.framework')
objc.loadBundleFunctions(bundle, globals(), (('SetSystemUIMode', b'III', " Sets the presentation mode for system-provided user interface elements."),))


kCGWindowListOptionOnScreenOnly = 1 << 0
kCGNullWindowID = 0
kCGWindowImageDefault = 0


class BlinkChatStream(ChatStream):
    priority = ChatStream.priority + 1
    accept_wrapped_types = ['text/*', 'image/*', 'application/im-iscomposing+xml', 'application/blink-icon', 'application/blink-zrtp-sas', 'application/blink-logging-status']

    def _create_local_media(self, uri_path):
        local_media = super(BlinkChatStream, self)._create_local_media(uri_path)
        local_media.attributes.append(SDPAttribute(b'blink-features', b'history-control icon'))
        return local_media


@implementer(IObserver)
class ChatController(MediaStream):
    type = "chat"

    chatViewController = objc.IBOutlet()
    smileyButton = objc.IBOutlet()

    splitView = objc.IBOutlet()
    splitViewFrame = None

    inputContainer = objc.IBOutlet()
    outputContainer = objc.IBOutlet()
    databaseLoggingButton = objc.IBOutlet()
    privateLabel = objc.IBOutlet()

    document = None
    fail_reason = None
    sessionController = None
    stream = None
    finishedLoading = False
    showHistoryEntries = 50
    mustShowUnreadMessages = False
    silence_notifications = False # don't send GUI notifications if active chat is not active

    history = None
    handler = None
    screensharing_handler = None

    session_was_active = False

    lastDeliveredTime = None
    undeliveredMessages = {} # id -> message

    # timer is reset whenever remote end sends is-composing active, when it times out, go to idle
    remoteTypingTimer = None

    drawerSplitterPosition = None
    mainViewSplitterPosition = None

    screenshot_task = None
    dealloc_timer = None
    zoom_period_label = ''
    message_count_from_history = 0

    nickname_request_map = {} # message id -> nickname
    chatOtrSmpWindow = None
    disable_chat_history = False
    remote_party_history = True
    video_attached = False
    media_started = False
    closed = False

    smp_verifified_using_zrtp = False
    smp_verification_delay = 0
    smp_verification_tries = 5
    smp_verification_question = b'What is the ZRTP authentication string?'

    @property
    def local_fingerprint(self):
        if self.stream.encryption.active:
            return self.stream.encryption.key_fingerprint.upper()
        else:
            return None

    @property
    def remote_fingerprint(self):
        if self.stream.encryption.active:
            return self.stream.encryption.peer_fingerprint.upper()
        else:
            return None
    
    @objc.python_method
    @classmethod
    def createStream(self):
        return BlinkChatStream()

    @objc.python_method
    def resetStream(self):
        self.sessionController.log_debug("Reset stream %s" % self)
        self.notification_center.discard_observer(self, sender=self.stream)
        self.media_started = False
        self.stream = BlinkChatStream()
        self.databaseLoggingButton.setHidden_(True)
        self.databaseLoggingButton.setState_(NSOffState)

    def initWithOwner_stream_(self, sessionController, stream):
        self = objc.super(ChatController, self).initWithOwner_stream_(sessionController, stream)
        sessionController.log_debug("Creating %s" % self)
        self.mediastream_ended = False
        self.session_succeeded = False
        self.last_failure_reason = None
        self.remoteIcon = None
        self.share_screen_in_conference = False

        self.previous_is_encrypted = False
        self.history_msgid_list=set()

        self.remote_uri = self.sessionController.remoteAOR if self.sessionController.account is not BonjourAccount() else self.sessionController.device_id

        self.local_uri = '%s@%s' % (self.sessionController.account.id.username, self.sessionController.account.id.domain) if self.sessionController.account is not BonjourAccount() else 'bonjour@local'
        
        BlinkLogger().log_info('Init chat controller %s -> %s' % (self.local_uri, self.remote_uri))

        self.silence_notifications = self.sessionController.contact.contact.silence_notifications if self.sessionController.contact is not None and isinstance(self.sessionController.contact, BlinkPresenceContact) else False
        
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, name='BlinkFileTransferDidEnd')
        self.notification_center.add_observer(self, name='ChatReplicationJournalEntryReceived')
        self.notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        self.notification_center.add_observer(self, name='BonjourAccountDidAddNeighbour')
        self.notification_center.add_observer(self, name='BonjourAccountDidUpdateNeighbour')
        self.notification_center.add_observer(self, name='BonjourAccountDidRemoveNeighbour')

        NSBundle.loadNibNamed_owner_("ChatView", self)

        self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("ChatView", "html"))
        if self.sessionController.account is BonjourAccount():
            self.chatViewController.setHandleScrolling_(False)
            self.chatViewController.lastMessagesLabel.setHidden_(True)

        settings = SIPSimpleSettings()
        if settings.chat.font_size < 0:
            i = settings.chat.font_size
            while i < 0:
                self.chatViewController.outputView.makeTextSmaller_(None)
                i += 1
        elif settings.chat.font_size > 0:
            i = settings.chat.font_size
            while i > 0:
                self.chatViewController.outputView.makeTextLarger_(None)
                i -= 1

        if self.sessionController.contact is not None and isinstance(self.sessionController.contact, BlinkPresenceContact) and self.sessionController.contact.contact.disable_smileys:
            self.chatViewController.expandSmileys = False
            self.chatViewController.toggleSmileys(self.chatViewController.expandSmileys)

        self.chatViewController.setAccount_(self.sessionController.account)
        self.chatViewController.resetRenderedMessages()

        self.outgoing_message_handler = OutgoingMessageHandler.alloc().initWithView_(self.chatViewController)

        self.screensharing_handler = ConferenceScreenSharingHandler()
        self.screensharing_handler.setDelegate(self)

        self.history = ChatHistory()
        self.backend = SIPManager()
        self.chatOtrSmpWindow = None

        if self.sessionController.contact is not None and isinstance(self.sessionController.contact, BlinkPresenceContact) and self.sessionController.contact.contact.disable_chat_history is not None:
            self.disable_chat_history = self.sessionController.contact.contact.disable_chat_history
        else:
            self.disable_chat_history = settings.chat.disable_history

        self.updateDatabaseRecordingButton()

        return self

    @property
    @objc.python_method
    def local_identity(self):
        if self.sessionController.account.display_name and self.sessionController.account.display_name != self.local_uri:
            return ChatIdentity.parse('%s <sip:%s>' % (self.sessionController.account.display_name, self.local_uri))
        else:
            return ChatIdentity.parse('<sip:%s>' % self.local_uri)

    @property
    @objc.python_method
    def remote_identity(self):
        if self.sessionController.display_name and self.sessionController.display_name != self.sessionController.remoteAOR:
            return ChatIdentity.parse('%s <sip:%s>' % (self.sessionController.display_name, self.sessionController.remoteAOR))
        else:
            return ChatIdentity.parse('<sip:%s>' % self.sessionController.remoteAOR)

    @objc.python_method
    def toggle_silence_notifications(self):
        if self.sessionController.contact:
            self.sessionController.contact.contact.silence_notifications = not self.sessionController.contact.contact.silence_notifications

        self.silence_notifications = not self.silence_notifications

    @objc.python_method
    def updateDatabaseRecordingButton(self):
        settings = SIPSimpleSettings()
        remote = self.sessionController.remoteAOR

        if self.remote_party_history and not self.disable_chat_history:
            self.privateLabel.setHidden_(True)
            self.databaseLoggingButton.setImage_(NSImage.imageNamed_("database-on"))
            self.databaseLoggingButton.setToolTip_(NSLocalizedString("Text conversation is saved to history database", "Tooltip"))
        elif not self.remote_party_history and not self.disable_chat_history:
            self.databaseLoggingButton.setImage_(NSImage.imageNamed_("database-remote-off"))
            self.privateLabel.setHidden_(False)
            self.databaseLoggingButton.setToolTip_(NSLocalizedString("%s wishes that text conversation is not saved in history database", "Tooltip text") % remote)
        else:
            self.privateLabel.setHidden_(False)
            self.databaseLoggingButton.setImage_(NSImage.imageNamed_("database-local-off"))
            self.databaseLoggingButton.setToolTip_(NSLocalizedString("Text conversation is not saved to history database", "Tooltip"))

    @property
    def otr_status(self):
        finished = self.stream.encryption.state is OTRState.Finished
        encrypted = self.stream.encryption.active
        trusted = self.stream.encryption.verified
        return (encrypted, trusted, finished)

    @property
    def is_encrypted(self):
        return self.stream.encryption.active

    @property
    def screensharing_allowed(self):
        try:
            return 'com.ag-projects.screen-sharing' in self.stream.chatroom_capabilities
        except AttributeError:
            return False

    @property
    def zrtp_sas_allowed(self):
        try:
            return 'com.ag-projects.zrtp-sas' in self.stream.chatroom_capabilities
        except AttributeError:
            return False

    @property
    def send_icon_allowed(self):
        if not self.stream:
            return false

        blink_features = self.stream.remote_media.attributes.getfirst(b'blink-features')
        blink_caps = blink_features.decode().split() if blink_features else []
        return 'icon' in blink_caps

    @property
    def history_control_allowed(self):
        if not self.stream:
            return false
        blink_features = self.stream.remote_media.attributes.getfirst(b'blink-features')
        blink_caps = blink_features.decode().split() if blink_features else []
        return 'history-control' in blink_caps

    @property
    def control_allowed(self):
        if not self.stream:
            return false

        try:
            return 'com.ag-projects.sylkserver-control' in self.stream.chatroom_capabilities
        except AttributeError:
            return False

    @property
    def chatWindowController(self):
        return NSApp.delegate().chatWindowController

    def awakeFromNib(self):
        # setup smiley popup
        smileys = SmileyManager().get_smiley_list()
        menu = self.smileyButton.menu()
        while menu.numberOfItems() > 0:
            menu.removeItemAtIndex_(0)
        bigText = NSAttributedString.alloc().initWithString_attributes_(" ", NSDictionary.dictionaryWithObject_forKey_(NSFont.systemFontOfSize_(16), NSFontAttributeName))
        for text, file in smileys:
            image = NSImage.alloc().initWithContentsOfFile_(file)
            if not image:
                BlinkLogger().log_info("cant load smiley file %s" % file)
                continue
            image.setScalesWhenResized_(True)
            image.setSize_(NSMakeSize(16, 16))
            atext = bigText.mutableCopy()
            atext.appendAttributedString_(NSAttributedString.alloc().initWithString_(text))
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(text, "insertSmiley:", "")
            menu.addItem_(item)
            item.setTarget_(self)
            item.setAttributedTitle_(atext)
            item.setRepresentedObject_(NSAttributedString.alloc().initWithString_(text))
            item.setImage_(image)

        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "drawerSplitViewDidResize:", NSSplitViewDidResizeSubviewsNotification, self.splitView)

    def drawerSplitViewDidResize_(self, notification):
        self.chatViewController.scrollToBottom()

    @objc.python_method
    def saveSplitterPosition(self):
        self.mainViewSplitterPosition={'output_frame': self.outputContainer.frame(), 'input_frame': self.inputContainer.frame()}

    @objc.python_method
    def restoreSplitterPosition(self):
        if self.mainViewSplitterPosition:
            self.outputContainer.setFrame_(self.mainViewSplitterPosition['output_frame'])
            self.inputContainer.setFrame_(self.mainViewSplitterPosition['input_frame'])

    @objc.python_method
    def getContentView(self):
        return self.chatViewController.view

    @objc.python_method
    def showSystemMessage(self, message, timestamp, is_error=False):
        if self.chatViewController:
            self.chatViewController.showSystemMessage(message, timestamp, is_error, call_id=self.sessionController.call_id)

    def insertSmiley_(self, sender):
        smiley = sender.representedObject()
        self.chatViewController.appendAttributedString_(smiley)

    @objc.python_method
    @run_in_gui_thread
    def changeStatus(self, newstate, fail_reason=None):
        MediaStream.changeStatus(self, self.status, newstate, fail_reason)
        self.status = newstate

    @objc.python_method
    def openChatWindow(self):
        old_session = self.chatWindowController.replaceInactiveWithCompatibleSession_(self.sessionController)
        if not old_session:
            view = self.getContentView()
            self.chatWindowController.addSession_withView_(self.sessionController, view)
        else:
            self.chatWindowController.selectSession_(self.sessionController)

        self.chatWindowController.window().makeKeyAndOrderFront_(None)
        self.chatWindowController.closing = False
        self.chatWindowController.addTimer()

        self.changeStatus(STREAM_IDLE)
        self.sessionController.setVideoConsumer("chat")

    @objc.python_method
    def closeWindow(self):
        self.chatWindowController.removeSession_(self.sessionController)
        if not self.chatWindowController.sessions:
            self.chatWindowController.window().orderOut_(None)

    @objc.python_method
    def startOutgoing(self, is_update):
        self.sessionController.log_debug("Start outgoing...")
        self.sessionController.video_consumer = "chat"
        self.session_succeeded = False
        self.last_failure_reason = None
        self.notification_center.add_observer(self, sender=self.stream)
        self.notification_center.add_observer(self, sender=self.sessionController)
        self.session_was_active = True
        self.mustShowUnreadMessages = True
        self.openChatWindow()
        if is_update and self.sessionController.canProposeMediaStreamChanges():
            self.changeStatus(STREAM_PROPOSING)
        else:
            self.changeStatus(STREAM_WAITING_DNS_LOOKUP)

    @objc.python_method
    def startIncoming(self, is_update):
        self.sessionController.log_debug("Start incoming...")
        self.sessionController.video_consumer = "chat"
        self.session_succeeded = False
        self.last_failure_reason = None
        self.notification_center.add_observer(self, sender=self.stream)
        self.notification_center.add_observer(self, sender=self.sessionController)
        self.session_was_active = True
        self.mustShowUnreadMessages = True
        self.openChatWindow()
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_INCOMING)

    @objc.python_method
    def sendFiles(self, fnames):
        filenames = [unicodedata.normalize('NFC', file) for file in fnames if os.path.isfile(file) or os.path.isdir(file)]
        if filenames:
            self.sessionControllersManager.send_files_to_contact(self.sessionController.account, self.sessionController.target_uri, filenames)
            return True
        return False

    @objc.python_method
    def sendOwnIcon(self):
        if not self.send_icon_allowed:
            return

        if self.sessionController.account is not BonjourAccount():
            return

        if self.stream:
            base64icon = encode_icon(self.chatWindowController.own_icon)
            if base64icon:
                self.stream.send_message(base64icon, content_type='application/blink-icon', timestamp=ISOTimestamp.now())

    @objc.python_method
    def sendLoggingState(self):
        if not self.history_control_allowed:
            return

        if self.status == STREAM_CONNECTED:
            content = 'enabled' if not self.disable_chat_history else 'disabled'
            self.stream.send_message(content, content_type='application/blink-logging-status', timestamp=ISOTimestamp.now())

    @objc.python_method
    def sendZRTPSas(self):
        if not self.zrtp_sas_allowed:
            return

        session = self.sessionController.session
        try:
            audio_stream = next(stream for stream in session.streams if stream.type=='audio' and stream.encryption.type=='ZRTP' and stream.encryption.active)
        except (StopIteration, TypeError):
            return
        full_local_path = self.stream.msrp.full_local_path
        full_remote_path = self.stream.msrp.full_remote_path
        sas = audio_stream.encryption.zrtp.sas
        if sas and self.stream and all(len(path)==1 for path in (full_local_path, full_remote_path)):
            self.stream.send_message(sas, 'application/blink-zrtp-sas')

    @objc.python_method
    def setNickname(self, nickname):
        if self.stream and self.stream.nickname_allowed:
            try:
                message_id = self.stream.set_local_nickname(nickname)
            except ChatStreamError:
                pass
            else:
                self.nickname_request_map[message_id] = nickname

    def validateToolbarItem_(self, item):
        return True

    @objc.IBAction
    def userClickedDatabaseLoggingButton_(self, sender):
        self.disable_chat_history = not self.disable_chat_history
        if self.sessionController.contact is not None and isinstance(self.sessionController.contact, BlinkPresenceContact):
            self.sessionController.contact.contact.disable_chat_history = self.disable_chat_history
            self.sessionController.contact.contact.save()
        self.updateDatabaseRecordingButton()
        self.sendLoggingState()

    def userClickedEncryptionMenu_(self, sender):
        tag = sender.tag()
        if tag == 4: # active
            if self.status == STREAM_CONNECTED:
                if self.is_encrypted:
                    self.sessionController.log_info("Chat encryption will stop")
                    self.stream.encryption.stop()
                else:
                    self.sessionController.log_info("Chat encryption requested")
                    self.stream.encryption.start()
                
        elif tag == 5: # verified
            self.stream.encryption.verified = not self.stream.encryption.verified

        elif tag == 9: # SMP window
            if self.stream.encryption.active:
                self.chatOtrSmpWindow.show()

        elif tag == 10:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("https://otr.cypherpunks.ca/Protocol-v3-4.0.0.html"))

        self.revalidateToolbar()

    @objc.python_method
    def revalidateToolbar(self):
        self.chatWindowController.revalidateToolbar()

    @objc.python_method
    @run_in_gui_thread
    def resetStyle(self):
        str_attributes = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.fontWithName_size_("Lucida Grande", 11), NSFontAttributeName)
        self.chatViewController.inputText.textStorage().setAttributedString_(NSAttributedString.alloc().initWithString_attributes_(" ", str_attributes))

    def textView_doCommandBySelector_(self, textView, selector):
        if selector == "insertNewline:" and self.chatViewController.inputText == textView:
            # attempt convert rich text to html
            try:
                # http://stackoverflow.com/questions/5298188/how-do-i-convert-nsattributedstring-into-html-string
                text_storage = textView.textStorage()
                exclude = ["doctype", "html", "head", "body", "xml"]
                documentAttributes = NSDictionary.dictionaryWithObjectsAndKeys_(NSHTMLTextDocumentType, NSDocumentTypeDocumentAttribute, exclude, NSExcludedElementsDocumentAttribute)
                data = text_storage.dataFromRange_documentAttributes_error_(NSMakeRange(0, text_storage.length()), documentAttributes, None)
                htmlData = NSData.alloc().initWithBytes_length_(data[0], len(data[0]))
                content = str(NSString.alloc().initWithData_encoding_(htmlData, NSUTF8StringEncoding))
                content_type = 'html'
            except Exception as e:
                content = str(textView.string())
                content_type = 'text'
                if content.endswith('\r\n'):
                    content = content[:-2]
                elif content.endswith('\n'):
                    content = content[:-1]

            if self.chatViewController.textWasPasted:
                # set style to default
                self.chatViewController.textWasPasted = False
                self.resetStyle()

            if content:
                self.chatViewController.inputText.setString_("")
                if self.outgoing_message_handler.send(content, recipient=self.remote_identity, content_type=content_type):
                    NotificationCenter().post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=NotificationData(direction='outgoing', history_entry=False, remote_party=self.sessionController.remoteAOR, local_party=format_identity_to_string(self.sessionController.account) if self.sessionController.account is not BonjourAccount() else 'bonjour@local', check_contact=True))

            if not self.stream or self.status in [STREAM_FAILED, STREAM_IDLE]:
                self.sessionController.log_info("Session not established, starting it")
                if self.outgoing_message_handler.messages:
                    # save unsend messages and pass them to the newly spawned handler
                    self.sessionController.pending_chat_messages = self.outgoing_message_handler.messages
                self.sessionController.startChatSession()
            self.chatViewController.resetTyping()
            return True
        return False

    def chatView_becameIdle_(self, chatView, time):
        if self.closed:
            return
        if self.stream:
            self.stream.send_composing_indication("idle", 60, last_active=time)

    def chatView_becameActive_(self, chatView, time):
        if self.closed:
            return

        if self.stream:
            self.stream.send_composing_indication("active", 60, last_active=time)
            if self.outgoing_message_handler.must_propose_otr:
                self.outgoing_message_handler.propose_otr()

    def chatViewDidLoad_(self, chatView):
        if self.closed:
            return

        self.replay_history()

    def isOutputFrameVisible(self):
        return True if self.outputContainer.frame().size.height > 10 else False

    @objc.python_method
    def scroll_back_in_time(self):
        try:
            msgid = self.history_msgid_list[0]
        except IndexError:
            msgid = None

        self.chatViewController.clear()
        self.chatViewController.resetRenderedMessages()
        self.replay_history(msgid)

    @objc.python_method
    @run_in_green_thread
    @allocate_autorelease_pool
    def replay_history(self, scrollToMessageId=None):
        if self.closed:
            return

        if self.sessionController is None:
            return

        blink_contact = self.sessionController.contact

        if not blink_contact or self.sessionController.account is BonjourAccount():
            remote_uris = self.remote_uri
        else:
            remote_uris = list(str(uri.uri) for uri in blink_contact.uris if '@' in uri.uri)
    
        zoom_factor = self.chatViewController.scrolling_zoom_factor
        if zoom_factor:
            period_array = {
                            1: datetime.datetime.now()-datetime.timedelta(days=2),
                            2: datetime.datetime.now()-datetime.timedelta(days=7),
                            3: datetime.datetime.now()-datetime.timedelta(days=31),
                            4: datetime.datetime.now()-datetime.timedelta(days=90),
                            5: datetime.datetime.now()-datetime.timedelta(days=180),
                            6: datetime.datetime.now()-datetime.timedelta(days=365),
                            7: datetime.datetime.now()-datetime.timedelta(days=3650)
                        }

            after_date = period_array[zoom_factor].strftime("%Y-%m-%d")

            if zoom_factor == 1:
                self.zoom_period_label = NSLocalizedString("Displaying messages from last day", "Label")
            elif zoom_factor == 2:
                self.zoom_period_label = NSLocalizedString("Displaying messages from last week", "Label")
            elif zoom_factor == 3:
                self.zoom_period_label = NSLocalizedString("Displaying messages from last month", "Label")
            elif zoom_factor == 4:
                self.zoom_period_label = NSLocalizedString("Displaying messages from last three months", "Label")
            elif zoom_factor == 5:
                self.zoom_period_label = NSLocalizedString("Displaying messages from last six months", "Label")
            elif zoom_factor == 6:
                self.zoom_period_label = NSLocalizedString("Displaying messages from last year", "Label")
            elif zoom_factor == 7:
                self.zoom_period_label = NSLocalizedString("Displaying all messages", "Label")
                self.chatViewController.setHandleScrolling_(False)

            results = self.history.get_messages(remote_uri=remote_uris, media_type=('chat', 'sms'), after_date=after_date, count=10000, search_text=self.chatViewController.search_text)
        else:
            results = self.history.get_messages(remote_uri=remote_uris, media_type=('chat', 'sms'), count=self.showHistoryEntries, search_text=self.chatViewController.search_text)

        # build a list of previously failed messages
        last_failed_messages=[]
        for row in results:
            if row.status == 'delivered':
                break
            last_failed_messages.append(row)
        last_failed_messages.reverse()
        self.history_msgid_list = [row.msgid for row in reversed(list(results))]

        # render last delievered messages except those due to be resent
        # messages_to_render = [row for row in reversed(list(results)) if row not in last_failed_messages]
        messages_to_render = [row for row in reversed(list(results))]
        #self.resend_last_failed_message(last_failed_messages)
        self.render_history_messages(messages_to_render, scrollToMessageId)

        self.send_pending_message()

    @objc.python_method
    @run_in_gui_thread
    def render_history_messages(self, messages, scrollToMessageId=None):
        if self.chatViewController.scrolling_zoom_factor:
            if not self.message_count_from_history:
                self.message_count_from_history = len(messages)
                self.chatViewController.lastMessagesLabel.setStringValue_(self.zoom_period_label)
            else:
                if self.message_count_from_history >= len(messages):
                    self.chatViewController.setHandleScrolling_(False)
                    self.zoom_period_label = NSLocalizedString("%s. There are no previous messages.", "Label") % self.zoom_period_label
                    self.chatViewController.lastMessagesLabel.setStringValue_(self.zoom_period_label)
                    self.chatViewController.setHandleScrolling_(False)
                else:
                    self.chatViewController.lastMessagesLabel.setStringValue_(self.zoom_period_label)
        else:
            self.message_count_from_history = len(messages)
            if len(messages):
                self.chatViewController.lastMessagesLabel.setStringValue_(NSLocalizedString("Scroll up for going back in time", "Label"))
            else:
                self.chatViewController.setHandleScrolling_(False)
                self.chatViewController.lastMessagesLabel.setStringValue_(NSLocalizedString("There are no previous messages", "Label"))

        if len(messages):
            message = messages[0]
            delta = datetime.date.today() - message.date

            if not self.chatViewController.scrolling_zoom_factor:
                if delta.days <= 2:
                    self.chatViewController.scrolling_zoom_factor = 1
                elif delta.days <= 7:
                    self.chatViewController.scrolling_zoom_factor = 2
                elif delta.days <= 31:
                    self.chatViewController.scrolling_zoom_factor = 3
                elif delta.days <= 90:
                    self.chatViewController.scrolling_zoom_factor = 4
                elif delta.days <= 180:
                    self.chatViewController.scrolling_zoom_factor = 5
                elif delta.days <= 365:
                    self.chatViewController.scrolling_zoom_factor = 6
                else:
                    self.chatViewController.scrolling_zoom_factor = 7

        call_id = None
        seen_sms = {}
        last_media_type = None

        cpim_re = re.compile(r'^(?:"?(?P<display_name>[^<]*[^"\s])"?)?\s*<(?P<uri>.+)>$')

        for message in messages:
            if message.status == 'sent':
                message.status = 'failed'

            if message.status == 'failed':
                continue

            if message.sip_callid != '' and message.media_type == 'sms':
                try:
                    seen_sms[message.sip_callid]
                except KeyError:
                    seen_sms[message.sip_callid] = True
                else:
                    continue

            if message.direction == 'outgoing':
                icon = NSApp.delegate().contactsWindowController.iconPathForSelf()
            else:
                sender_uri = sipuri_components_from_string(message.cpim_from)[0]
                icon = NSApp.delegate().contactsWindowController.iconPathForURI(sender_uri)

            timestamp=ISOTimestamp(message.cpim_timestamp)
            is_html = message.content_type != 'text'
            private = bool(int(message.private))

            if self.chatViewController:
                #if call_id is not None and call_id != message.sip_callid and  message.media_type == 'chat':
                    #self.chatViewController.showSystemMessage('Connection established', timestamp, False, call_id=message.sip_callid,)

                #if message.media_type == 'sms' and last_media_type == 'chat':
                    #self.chatViewController.showSystemMessage('Short messages', timestamp, False, call_id=message.sip_callid,)

                sender = message.cpim_from
                recipient = message.cpim_to

                match = cpim_re.match(sender)
                if match:
                    sender = match.group('display_name') or match.group('uri')

                match = cpim_re.match(recipient)
                if match:
                    recipient = match.group('display_name') or match.group('uri')
                    
                self.chatViewController.showMessage(message.sip_callid, message.msgid, message.direction, sender, icon, message.body, timestamp, is_private=private, recipient=recipient, state=message.status, is_html=is_html, history_entry=True, media_type = message.media_type, encryption=message.encryption)

            call_id = message.sip_callid
            last_media_type = 'chat' if message.media_type == 'chat' else 'sms'

        if scrollToMessageId is not None:
            self.chatViewController.scrollToId(scrollToMessageId)

        self.chatViewController.loadingProgressIndicator.stopAnimation_(None)
        self.chatViewController.loadingTextIndicator.setStringValue_("")

    @objc.python_method
    @run_in_gui_thread
    def resend_last_failed_message(self, messages):
        if self.sessionController.account is BonjourAccount():
            return

        for message in messages:
            private = True if message.private == "1" else False
            self.outgoing_message_handler.resend(message.msgid, message.body, self.remote_identity, private, message.content_type)

    @objc.python_method
    @run_in_gui_thread
    def send_pending_message(self):
        if self.sessionController.pending_chat_messages:
            for message in reversed(list(self.sessionController.pending_chat_messages.values())):
                self.outgoing_message_handler.resend(message.msgid, message.content, message.recipient, message.private)
            self.sessionController.pending_chat_messages = {}

    def chatViewDidGetNewMessage_(self, chatView):
        NSApp.delegate().noteNewMessage(self.chatViewController.outputView.window())
        if self.mustShowUnreadMessages:
            self.chatWindowController.noteNewMessageForSession_(self.sessionController)

    @objc.python_method
    def updateEncryptionWidgets(self):
        if self.status == STREAM_CONNECTED:
            if self.is_encrypted:
                if self.stream.encryption.verified:
                    self.chatWindowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-green"))
                else:
                    self.chatWindowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-red"))
            else:
                self.chatWindowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("unlocked-darkgray"))
            # TODO: use orange if first time
            # self.chatWindowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-orange"))
        else:
            self.chatWindowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("unlocked-darkgray"))

    @objc.python_method
    def connectButtonEnabled(self):
        if '@127.0.0.1' in str(self.remote_identity.uri):
            return False

        if self.status in (STREAM_IDLE, STREAM_WAITING_DNS_LOOKUP, STREAM_CONNECTING, STREAM_CONNECTED):
            return True
        elif self.status == STREAM_PROPOSING:
            return self.sessionController.proposalOriginator == 'local'
        elif self.status == STREAM_DISCONNECTING:
            return False
        else:
            return self.sessionController.canProposeMediaStreamChanges() or self.sessionController.canStartSession()

    @objc.python_method
    def audioButtonEnabled(self):
        if '@127.0.0.1' in str(self.remote_identity.uri):
            return False

        if self.status in (STREAM_WAITING_DNS_LOOKUP, STREAM_CONNECTING, STREAM_PROPOSING, STREAM_DISCONNECTING, STREAM_CANCELLING):
            return False

        if self.sessionController.hasStreamOfType("audio"):
            audio_stream = self.sessionController.streamHandlerOfType("audio")
            if audio_stream.status == STREAM_FAILED:
                return False
            if audio_stream.status == STREAM_CONNECTED:
                return self.sessionController.canProposeMediaStreamChanges() or self.sessionController.canStartSession()
            elif audio_stream.status in (STREAM_PROPOSING, STREAM_RINGING):
                return True if self.sessionController.canCancelProposal() else False
            else:
                return True if self.sessionController.canProposeMediaStreamChanges() and self.status in (STATE_IDLE, STREAM_CONNECTED) else False
        else:
            return self.sessionController.canProposeMediaStreamChanges() or self.sessionController.canStartSession()

    @objc.python_method
    def fileTransferButtonEnabled(self):
        if '@127.0.0.1' in str(self.remote_identity.uri):
            return False
        
        return True

    @objc.python_method
    def videoButtonEnabled(self):
        if '@127.0.0.1' in str(self.remote_identity.uri):
            return False

        if self.status in (STREAM_WAITING_DNS_LOOKUP, STREAM_CONNECTING, STREAM_PROPOSING, STREAM_DISCONNECTING, STREAM_CANCELLING):
            return False
            
        if self.sessionController.hasStreamOfType("video"):
            video_stream = self.sessionController.streamHandlerOfType("video")
            if video_stream.status == STREAM_FAILED:
                return False
            if video_stream.status == STREAM_CONNECTED:
                return self.sessionController.canProposeMediaStreamChanges() or self.sessionController.canStartSession()
            elif video_stream.status in (STREAM_PROPOSING, STREAM_RINGING):
                return True if self.sessionController.canCancelProposal() else False
            else:
                return True if self.sessionController.canProposeMediaStreamChanges() and self.status in (STATE_IDLE, STREAM_CONNECTED) else False
        else:
            return self.sessionController.canProposeMediaStreamChanges() or self.sessionController.canStartSession()

    @objc.python_method
    def updateToolbarButtons(self, toolbar, got_proposal=False):
        """Called by ChatWindowController when receiving various middleware notifications"""
        settings = SIPSimpleSettings()

        audio_stream = self.sessionController.streamHandlerOfType("audio")

        for item in toolbar.visibleItems():
            identifier = item.itemIdentifier()
            if identifier == 'encryption':
                self.updateEncryptionWidgets()
                item.setEnabled_(True)

            elif identifier == 'connect_button':
                if self.status in (STREAM_CONNECTING, STREAM_WAITING_DNS_LOOKUP):
                    item.setToolTip_(NSLocalizedString("Cancel Chat", "Tooltip"))
                    item.setLabel_(NSLocalizedString("Cancel", "Button title"))
                    item.setImage_(NSImage.imageNamed_("stop_chat"))
                elif self.status == STREAM_PROPOSING:
                    if self.sessionController.proposalOriginator != 'remote':
                        item.setToolTip_(NSLocalizedString("Cancel Chat", "Tooltip"))
                        item.setLabel_(NSLocalizedString("Cancel", "Button title"))
                        item.setImage_(NSImage.imageNamed_("stop_chat"))
                elif self.status == STREAM_CONNECTED:
                    item.setToolTip_(NSLocalizedString("End chat", "Tooltip"))
                    item.setLabel_(NSLocalizedString("Disconnect", "Button title"))
                    item.setImage_(NSImage.imageNamed_("stop_chat"))
                else:
                    item.setToolTip_(NSLocalizedString("Start chat", "Tooltip"))
                    item.setLabel_(NSLocalizedString("Connect", "Button title"))
                    item.setImage_(NSImage.imageNamed_("start_chat"))
                    
                item.setEnabled_(self.connectButtonEnabled())

            elif identifier == 'audio':
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_CONNECTED:
                        item.setToolTip_(NSLocalizedString("Remove audio", "Tooltip"))
                        item.setImage_(NSImage.imageNamed_("hangup"))
                    elif audio_stream.status in (STREAM_PROPOSING, STREAM_RINGING):
                        item.setToolTip_(NSLocalizedString("Cancel Audio", "Tooltip"))
                        item.setImage_(NSImage.imageNamed_("hangup"))
                else:
                    if self.sessionController.state == STATE_IDLE:
                        item.setToolTip_(NSLocalizedString("Start Audio Call", "Tooltip"))
                    else:
                        item.setToolTip_(NSLocalizedString("Add audio", "Tooltip"))
                    item.setImage_(NSImage.imageNamed_("audio"))
            elif identifier == 'video':
                if self.sessionController.hasStreamOfType("video"):
                    video_stream = self.sessionController.streamHandlerOfType("video")
                    if video_stream.status == STREAM_CONNECTED:
                        item.setToolTip_(NSLocalizedString("Remove video", "Tooltip"))
                    elif video_stream.status in (STREAM_PROPOSING, STREAM_RINGING):
                        item.setToolTip_(NSLocalizedString("Cancel Video", "Tooltip"))
                else:
                    if self.sessionController.state == STATE_IDLE:
                        item.setToolTip_(NSLocalizedString("Start Video Call", "Tooltip"))
                    else:
                        item.setToolTip_(NSLocalizedString("Add video", "Tooltip"))

            elif identifier == 'hold':
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_CONNECTED:
                        if audio_stream.holdByRemote:
                            item.setToolTip_(NSLocalizedString("On Hold", "Tooltip"))
                            item.setImage_(NSImage.imageNamed_("paused"))
                        elif audio_stream.holdByLocal:
                            item.setToolTip_(NSLocalizedString("Unhold", "Tooltip"))
                            item.setImage_(NSImage.imageNamed_("paused"))
                        else:
                            item.setToolTip_(NSLocalizedString("Hold", "Tooltip"))
                            item.setImage_(NSImage.imageNamed_("pause"))
                    else:
                        item.setImage_(NSImage.imageNamed_("pause"))
                else:
                    item.setImage_(NSImage.imageNamed_("pause"))
            elif identifier == 'record':
                if audio_stream:
                    if audio_stream.status == STREAM_CONNECTED and audio_stream.stream.recorder is not None and audio_stream.stream.recorder.is_active:
                        item.setImage_(NSImage.imageNamed_("recording1"))
                        item.setToolTip_(NSLocalizedString("Stop Recording", "Tooltip"))
                    else:
                        item.setToolTip_(NSLocalizedString("Start Recording", "Tooltip"))
                        item.setImage_(NSImage.imageNamed_("record"))
                else:
                    item.setToolTip_(NSLocalizedString("Start Recording", "Tooltip"))
            elif identifier == 'screen':
                if self.sessionController.remote_focus:
                    item.setEnabled_(True if self.status == STREAM_CONNECTED and self.screensharing_allowed else False)
                else:
                    item.setEnabled_(True if self.status == STREAM_CONNECTED else False)
                self.setScreenSharingToolbarIcon()
            elif identifier == 'smileys':
                item.setImage_(NSImage.imageNamed_("smiley_on" if self.chatViewController.expandSmileys else "smiley_off"))
                item.setEnabled_(True)
            elif identifier == 'editor' and self.sessionController.account is not BonjourAccount() and not settings.chat.disable_collaboration_editor:
                item.setImage_(NSImage.imageNamed_("editor-changed" if not self.chatViewController.editorVisible and self.chatViewController.editorIsComposing else "editor"))
            elif identifier == 'screenshot':
                item.setEnabled_(self.sessionControllersManager.isMediaTypeSupported('file-transfer'))
            elif identifier == 'sendfile':
                item.setEnabled_(self.sessionControllersManager.isMediaTypeSupported('file-transfer'))

    @objc.python_method
    def validateToolbarButton(self, item):
        """
        Called automatically by Cocoa in ChatWindowController to enable/disable each toolbar item
        """

        if self.sessionController.hasStreamOfType("audio"):
            audio_stream = self.sessionController.streamHandlerOfType("audio")

        if hasattr(item, 'itemIdentifier'):
            identifier = item.itemIdentifier()
            if identifier == NSToolbarPrintItemIdentifier and NSApp.delegate().chat_print_enabled:
                return True

            if identifier == 'encryption':
                self.updateEncryptionWidgets()
                return True

            elif identifier == 'connect_button':
                _chat_enabled = self.connectButtonEnabled()
                return _chat_enabled
            elif identifier == 'audio':
                _audio_enabled = self.audioButtonEnabled()
                return _audio_enabled
            elif identifier == 'hold':
                _audio_enabled = self.audioButtonEnabled()
                if self.sessionController.hasStreamOfType("audio") and audio_stream.status == STREAM_CONNECTED:
                    return True and _audio_enabled
            elif identifier == 'record':
                return True if self.sessionController.hasStreamOfType("audio") and audio_stream.status == STREAM_CONNECTED and NSApp.delegate().recording_enabled else False
            elif identifier == 'video':
                _video_enabled = self.videoButtonEnabled()
                if self.sessionController.hasStreamOfType("video"):
                    video_stream = self.sessionController.streamHandlerOfType("video")
                    if video_stream.status in (STREAM_CONNECTED, STREAM_PROPOSING, STREAM_RINGING):
                        item.setImage_(NSImage.imageNamed_("video-active"))
                    else:
                        item.setImage_(NSImage.imageNamed_("video"))
                else:
                    item.setImage_(NSImage.imageNamed_("video"))
                return _video_enabled
            elif identifier == 'sendfile' and self.sessionControllersManager.isMediaTypeSupported('file-transfer'):
                return self.fileTransferButtonEnabled()
            elif identifier == 'smileys':
                return True
            elif identifier == 'editor' and self.sessionController.account is not BonjourAccount():
                settings = SIPSimpleSettings()
                if not settings.chat.disable_collaboration_editor:
                    return True
            elif identifier == 'history' and NSApp.delegate().history_enabled:
                return True
            elif identifier == 'screen':
                if self.sessionController.remote_focus:
                    self.chatWindowController.screenSharingPopUpButton.setMenu_(self.chatWindowController.conferenceScreenSharingMenu)
                    self.chatWindowController.conferenceScreenSharingMenu.itemAtIndex_(0).setImage_(NSImage.imageNamed_("display_red" if self.share_screen_in_conference else "display"))
                    return self.status == STREAM_CONNECTED
                else:
                    self.chatWindowController.screenSharingPopUpButton.setMenu_(self.chatWindowController.screenShareMenu)
                    self.chatWindowController.conferenceScreenSharingMenu.itemAtIndex_(0).setImage_(NSImage.imageNamed_("display_red" if self.sessionController.hasStreamOfType("screen-sharing") else "display"))
                    return True
            elif identifier == 'screenshot':
                return True

        return False

    @objc.python_method
    def userClickedToolbarButton(self, sender):
        """
        Called by ChatWindowController when dispatching toolbar button clicks to the selected Session tab
        """
        if hasattr(sender, 'itemIdentifier'):
            # regular toolbar items (except buttons and menus)
            settings = SIPSimpleSettings()
            identifier = sender.itemIdentifier()

            if self.sessionController.hasStreamOfType("audio"):
                audio_stream = self.sessionController.streamHandlerOfType("audio")

            if self.sessionController.hasStreamOfType("video"):
                video_stream = self.sessionController.streamHandlerOfType("video")

            if identifier == 'connect_button':
                if self.status in (STREAM_CONNECTED, STREAM_CONNECTING, STREAM_PROPOSING, STREAM_WAITING_DNS_LOOKUP):
                    self.end()
                else:
                    if self.sessionController.canProposeMediaStreamChanges() or self.sessionController.canStartSession():
                        if self.status in (STREAM_IDLE, STREAM_FAILED):
                            self.sessionController.startChatSession()
                    else:
                        self.sessionController.log_info("Session has a pending proposal")

            elif identifier == 'audio':
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_PROPOSING:
                        self.sessionController.cancelProposal(audio_stream)
                    else:
                        if self.status == STREAM_CONNECTED:
                            self.sessionController.removeAudioFromSession()
                            sender.setToolTip_(NSLocalizedString("Add audio", "Tooltip"))
                        else:
                            self.sessionController.endStream(audio_stream)
                            sender.setToolTip_(NSLocalizedString("Start audio", "Tooltip"))

                    sender.setImage_(NSImage.imageNamed_("audio"))

                    # The button will be enabled again after operation is finished
                    sender.setEnabled_(False)
                else:
                    if self.sessionController.state == STATE_IDLE:
                        self.notification_center.add_observer(self, sender=self.sessionController)
                        self.sessionController.startCompositeSessionWithStreamsOfTypes(("audio", "chat"))
                    else:
                        self.sessionController.addAudioToSession()

                    sender.setToolTip_(NSLocalizedString("Cancel Audio", "Tooltip"))
                    sender.setImage_(NSImage.imageNamed_("hangup"))

            elif identifier == 'video':
                if self.sessionController.hasStreamOfType("video"):
                    if video_stream.status == STREAM_PROPOSING:
                        self.sessionController.cancelProposal(video_stream)
                        self.sessionController.setVideoConsumer(None)
                    else:
                        if video_stream.status == STREAM_CONNECTED:
                            self.sessionController.removeVideoFromSession()
                            sender.setToolTip_(NSLocalizedString("Add Video", "Tooltip"))
                        else:
                            self.sessionController.endStream(video_stream)
                            sender.setToolTip_(NSLocalizedString("Start video", "Tooltip"))
                    
                    # The button will be enabled again after operation is finished
                    sender.setEnabled_(False)
                else:
                    if self.sessionController.state == STATE_IDLE:
                        self.notification_center.add_observer(self, sender=self.sessionController)
                        self.sessionController.startCompositeSessionWithStreamsOfTypes(("audio", "video", "chat"))
                    else:
                        self.sessionController.addVideoToSession()
                    self.sessionController.setVideoConsumer("chat")
                    sender.setToolTip_(NSLocalizedString("Cancel Video", "Tooltip"))
                    sender.setImage_(NSImage.imageNamed_("video"))


            elif identifier == 'record' and NSApp.delegate().recording_enabled:
                if audio_stream and audio_stream.stream.recorder is not None and audio_stream.stream.recorder.is_active:
                    audio_stream.stream.stop_recording()
                    sender.setImage_(NSImage.imageNamed_("record"))
                else:
                    session = self.sessionController.session
                    direction = session.direction
                    remote = "%s@%s" % (session.remote_identity.uri.user, session.remote_identity.uri.host)
                    filename = "%s-%s-%s.wav" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), remote, direction)
                    path = os.path.join(settings.audio.directory.normalized, session.account.id)
                    audio_stream.stream.start_recording(os.path.join(path, filename))
                    sender.setImage_(NSImage.imageNamed_("recording1"))

            elif identifier == 'hold' and self.sessionController.hasStreamOfType("audio") and not self.sessionController.inProposal:
                # TODO: put video on hold -adi
                if audio_stream.holdByLocal:
                    audio_stream.unhold()
                    audio_stream.view.setSelected_(True)
                    sender.setImage_(NSImage.imageNamed_("pause"))
                else:
                    sender.setImage_(NSImage.imageNamed_("paused"))
                    audio_stream.hold()

            elif identifier == 'sendfile':
                openFileTransferSelectionDialog(self.sessionController.account, self.sessionController.target_uri)

            elif identifier == 'smileys':
                self.chatViewController.expandSmileys = not self.chatViewController.expandSmileys
                sender.setImage_(NSImage.imageNamed_("smiley_on" if self.chatViewController.expandSmileys else "smiley_off"))
                self.chatViewController.toggleSmileys(self.chatViewController.expandSmileys)
                blink_contact = self.sessionController.contact
                if blink_contact:
                    blink_contact.contact.disable_smileys = not blink_contact.contact.disable_smileys
                    blink_contact.contact.save()

            elif identifier == 'editor' and self.sessionController.account is not BonjourAccount() and not settings.chat.disable_collaboration_editor:
                self.toggleEditor()
                sender.setImage_(NSImage.imageNamed_("editor"))
                sender.setToolTip_(NSLocalizedString("Switch back to chat session", "Tooltip") if self.chatViewController.editorVisible else NSLocalizedString("Show collaborative editor", "Tooltip"))
            elif identifier == 'history' and NSApp.delegate().history_enabled:
                contactWindow = NSApp.delegate().contactsWindowController
                contactWindow.showHistoryViewer_(None)
                if self.sessionController.account is BonjourAccount():
                    contactWindow.historyViewer.filterByURIs(('bonjour@local', ))
                else:
                    contactWindow.historyViewer.filterByURIs((self.remote_uri, ))
                days = 1
                if self.chatViewController.scrolling_zoom_factor:
                    if self.chatViewController.scrolling_zoom_factor == 1:
                        days = 1
                    elif self.chatViewController.scrolling_zoom_factor == 2:
                        days = 7
                    elif self.chatViewController.scrolling_zoom_factor == 3:
                        days = 31
                    elif self.chatViewController.scrolling_zoom_factor == 4:
                        days = 90
                    elif self.chatViewController.scrolling_zoom_factor == 5:
                        days = 180
                    elif self.chatViewController.scrolling_zoom_factor == 6:
                        days = 365
                    elif self.chatViewController.scrolling_zoom_factor == 7:
                        days = 3650
                contactWindow.historyViewer.setPeriod(days)

    def userClickedSnapshotMenu_(self, sender):
        if NSApp.delegate().contactsWindowController.sessionControllersManager.isMediaTypeSupported('video'):
            picker = PhotoPicker(ApplicationData.get('.tmp_snapshots'), high_res=True, history=False)
            path, image = picker.runModal()
            if image and path:
                self.sendFiles([str(path)])

    def userClickedScreenshotMenu_(self, sender):
        screenshots_folder = ApplicationData.get('.tmp_screenshots')
        if not os.path.exists(screenshots_folder):
            os.mkdir(screenshots_folder, 0o700)
        filename = '%s/%s_screencapture_%s.png' % (screenshots_folder, datetime.datetime.now(tzlocal()).strftime("%Y-%m-%d_%H-%M"), self.sessionController.account.id)
        basename, ext = os.path.splitext(filename)
        i = 1
        while os.path.exists(filename):
            filename = '%s_%d%s' % (basename, i, ext)
            i += 1

        self.screencapture_file = filename
        self.screenshot_task = NSTask.alloc().init()
        self.screenshot_task.setLaunchPath_('/usr/sbin/screencapture')
        if sender.tag() == TOOLBAR_SCREENSHOT_MENU_WINDOW:
            self.screenshot_task.setArguments_(['-W', '-tpng', self.screencapture_file])
        elif sender.tag() == TOOLBAR_SCREENSHOT_MENU_AREA:
            self.screenshot_task.setArguments_(['-s', '-tpng', self.screencapture_file])
        else:
            return

        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "checkScreenshotTaskStatus:", NSTaskDidTerminateNotification, self.screenshot_task)
        self.chatWindowController.window().orderBack_(None)
        self.screenshot_task.launch()

    def userClickedConferenceScreenSharingQualityMenu_(self, sender):
        if sender.tag() == TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_HIGH:
            if self.screensharing_handler.connected:
                self.screensharing_handler.setQuality('high')
        elif sender.tag() == TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_LOW:
            if self.screensharing_handler.connected:
                self.screensharing_handler.setQuality('low')
        elif sender.tag() == TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_MEDIUM:
            if self.screensharing_handler.connected:
                self.screensharing_handler.setQuality('medium')

    def userClickedScreenSharingMenu_(self, sender):
        if sender.tag() == TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL and self.status == STREAM_CONNECTED:
            if not self.sessionController.remote_focus:
                if not self.sessionController.hasStreamOfType("screen-sharing"):
                    self.sessionController.addMyScreenToSession()
                    sender.setEnabled_(False)
            else:
                self.toggleScreensharingWithConferenceParticipants()
        elif sender.tag() == TOOLBAR_SCREENSHARING_MENU_REQUEST_REMOTE and self.status == STREAM_CONNECTED:
            if not self.sessionController.hasStreamOfType("screen-sharing"):
                self.sessionController.addRemoteScreenToSession()
                sender.setEnabled_(False)
        elif sender.tag() == TOOLBAR_SCREENSHARING_MENU_CANCEL and self.status == STREAM_CONNECTED:
            if self.sessionController.hasStreamOfType("screen-sharing"):
                screen_sharing_stream = self.sessionController.streamHandlerOfType("screen-sharing")
                if screen_sharing_stream.status == STREAM_PROPOSING or screen_sharing_stream.status == STREAM_RINGING:
                    self.sessionController.cancelProposal(screen_sharing_stream)
                elif screen_sharing_stream.status == STREAM_CONNECTED:
                    self.sessionController.removeScreenFromSession()

    @objc.python_method
    def toggleScreensharingWithConferenceParticipants(self):
        self.share_screen_in_conference = True if not self.share_screen_in_conference else False
        if self.share_screen_in_conference and self.stream is not None:
            self.sessionController.log_info("Start sharing screen with conference participants")
            self.screensharing_handler.setConnected(self.stream)
        else:
            self.screensharing_handler.setDisconnected()
            self.sessionController.log_info("Stop sharing screen with conference participants")

        self.setScreenSharingToolbarIcon()

    @objc.python_method
    def setScreenSharingToolbarIcon(self):
        if self.sessionController.remote_focus:
            menu = self.chatWindowController.conferenceScreenSharingMenu
            self.chatWindowController.screenSharingPopUpButton.setMenu_(menu)
            self.chatWindowController.conferenceScreenSharingMenu.itemAtIndex_(0).setImage_(NSImage.imageNamed_("display_red" if self.share_screen_in_conference else "display"))
        else:
            menu = self.chatWindowController.screenShareMenu
            self.chatWindowController.screenSharingPopUpButton.setMenu_(menu)
            self.chatWindowController.screenShareMenu.itemAtIndex_(0).setImage_(NSImage.imageNamed_("display_red" if self.sessionController.hasStreamOfType("screen-sharing") else "display"))

        menu = self.chatWindowController.conferenceScreenSharingMenu
        menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL).setTitle_(NSLocalizedString("Share My Screen with Conference Participants", "Menu item") if self.share_screen_in_conference == False else NSLocalizedString("Stop Screen Sharing", "Menu item"))
        self.chatWindowController.noteSession_isScreenSharing_(self.sessionController, self.share_screen_in_conference)

        self.chatWindowController.setScreenSharingToolbarIconSize()

    @objc.python_method
    def resetEditorToolbarIcon(self):
        try:
            item = next((item for item in self.chatWindowController.toolbar.visibleItems() if item.tag() == 109))
        except StopIteration:
            pass
        else:
            item.setImage_(NSImage.imageNamed_("editor"))

    def checkScreenshotTaskStatus_(self, notification):
        status = notification.object().terminationStatus()
        if status == 0 and self.sessionController and os.path.exists(self.screencapture_file):
            self.sendFiles([str(self.screencapture_file)])
        NSNotificationCenter.defaultCenter().removeObserver_name_object_(self, NSTaskDidTerminateNotification, self.screenshot_task)
        self.chatWindowController.window().orderFront_(None)
        self.screenshot_task = None

    @objc.python_method
    def toggleEditor(self):
        self.chatViewController.editorIsComposing = False
        self.chatViewController.toggleCollaborationEditor()
        self.chatWindowController.noteSession_isComposing_(self.sessionController, False)

    def remoteBecameIdle_(self, timer):
        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
        self.remoteTypingTimer = None
        self.chatWindowController.noteSession_isComposing_(self.sessionController, False)

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        try:
            handler(notification.sender, notification.data)
        except Exception:
            self.sessionController.log_error(traceback.format_exc())

    @objc.python_method
    def _do_smp_verification(self):
        session = self.sessionController.session
        try:
            audio_stream = next(stream for stream in session.streams if stream.type=='audio' and stream.encryption.type=='ZRTP' and stream.encryption.active and stream.encryption.zrtp.verified)
        except StopIteration:
            audio_stream = None

        if audio_stream:
            self.stream.encryption.smp_verify(audio_stream.encryption.zrtp.sas, question=self.smp_verification_question)
            self.sessionController.log_info("Performing OTR SMP verification using ZRTP SAS...")

    @objc.python_method
    def _NH_ChatStreamSMPVerificationDidStart(self, stream, data):
        if data.originator == 'remote':
            self.sessionController.log_info("OTR SMP verification requested by remote")
            if data.question == self.smp_verification_question:
                session = self.sessionController.session
                try:
                    audio_stream = next(stream for stream in session.streams if stream.type=='audio' and stream.encryption.type=='ZRTP' and stream.encryption.active and stream.encryption.zrtp.verified)
                except StopIteration:
                    self.stream.encryption.smp_abort()
                else:
                    self.sessionController.log_info("OTR SMP verification done automatically using ZRTP SAS")
                    self.stream.encryption.smp_answer(audio_stream.encryption.zrtp.sas)
                    self.smp_verifified_using_zrtp = True
            else:
                self.chatOtrSmpWindow.show(question=data.question, remote=True)
        else:
            self._do_smp_verification()

    @objc.python_method
    def _NH_ChatStreamSMPVerificationDidNotStart(self, stream, data):
        self.sessionController.log_info("OTR SMP verification did not start: %s" % data.reason)
        self.chatOtrSmpWindow.handle_remote_response()

    @objc.python_method
    def _NH_ChatStreamSMPVerificationDidEnd(self, stream, data):
        self.sessionController.log_info("OTR SMP verification ended")
        if data.status is SMPStatus.Success:
            result = self.chatOtrSmpWindow.handle_remote_response(data.same_secrets)
            if result:
                pass
                #self.showSystemMessage('Peer identity verification succeeded', ISOTimestamp.now(), False)
            else:
                self.showSystemMessage('Please validate the identity in the encryption lock menu', ISOTimestamp.now(), True)

        elif data.status is SMPStatus.Interrupted:
            self.sessionController.log_info("OTR SMP verification aborted: %s" % data.reason)
        elif data.status is SMPStatus.ProtocolError:
            self.sessionController.log_info("OTR SMP verification error: %s" % data.reason)
            if data.reason == 'startup collision':
                self.smp_verification_tries -= 1
                self.smp_verification_delay *= 2
                if self.smp_verification_tries > 0:
                    call_later(self.smp_verification_delay, self._do_smp_verification)

    @objc.python_method
    def _NH_ChatStreamOTRError(self, stream, data):
        self.sessionController.log_info("Chat encryption error: %s", data.error)

    @objc.python_method
    def _NH_ChatStreamOTREncryptionStateChanged(self, stream, data):
        if data.new_state is OTRState.Encrypted:
            #self.showSystemMessage("Encryption enabled", ISOTimestamp.now())
            self.sessionController.log_info("Chat encryption activated using OTR protocol")
            self.sessionController.log_info("OTR local fingerprint %s" % self.local_fingerprint)
            self.sessionController.log_info("OTR remote fingerprint %s" % self.remote_fingerprint)
            if stream.encryption.verified:
                self.sessionController.log_info("OTR remote fingerprint has been verified")
            else:
                self.sessionController.log_error("OTR remote fingerprint has not yet been verified")
                self.smp_verification_delay = 0 if self.stream.encryption.key_fingerprint > self.stream.encryption.peer_fingerprint else 1
                self.smp_verification_tries = 5
                self._do_smp_verification()

            self.chatViewController.hideEncryptionFinishedConfirmationDialog()
        elif data.new_state is OTRState.Finished:
            log = NSLocalizedString("Chat encryption finished", "Label")
            self.sessionController.log_info("Chat encryption deactivated")
            nc_title = NSLocalizedString("Encryption", "System notification title")
            self.showSystemMessage("Encryption disabled", ISOTimestamp.now(), True)
            nc_subtitle = self.sessionController.titleShort
            NSApp.delegate().gui_notify(nc_title, log, nc_subtitle)
            self.chatViewController.showEncryptionFinishedConfirmationDialog()
        elif data.new_state is OTRState.Plaintext:
            log = NSLocalizedString("Chat encryption deactivated", "Label")
            self.sessionController.log_info("Chat encryption deactivated")
            self.showSystemMessage("Encryption deactivated", ISOTimestamp.now(), True)
            nc_title = NSLocalizedString("Encryption", "System notification title")
            nc_subtitle = self.sessionController.titleShort
            NSApp.delegate().gui_notify(nc_title, log, nc_subtitle)

        self.updateEncryptionWidgets()

    @objc.python_method
    def _NH_BonjourAccountDidAddNeighbour(self, sender, data):
        record = data.record
        print('Add bonjour %s %s' % (record.uri, type(record.uri)))
        if self.remote_uri == record.id:
            self.sessionController.target_uri = record.uri
            self.sessionController.display_name = record.name
            self.revalidateToolbar()
            BlinkLogger().log_info('Update chat controller %s -> %s' % (self.local_uri, self.remote_uri))

    @objc.python_method
    def _NH_BonjourAccountDidUpdateNeighbour(self, sender, data):
        record = data.record
        if self.remote_uri == record.id:
            self.sessionController.target_uri = record.uri
            self.sessionController.display_name = record.name
            self.revalidateToolbar()
            BlinkLogger().log_info('Update chat controller %s -> %s' % (self.local_uri, self.remote_uri))

    @objc.python_method
    def _NH_BonjourAccountDidRemoveNeighbour(self, sender, data):
        record = data.record
        if self.remote_uri == record.id:
            new_target = 'sip:' + ''.join(random.sample(string.ascii_letters+string.digits, 8)) + '@127.0.0.1:5060'
            self.sessionController.target_uri = SIPURI.parse(new_target)
            self.revalidateToolbar()
            BlinkLogger().log_info('Update chat controller %s -> %s' % (self.local_uri, self.remote_uri))

    @objc.python_method
    def _NH_ChatStreamGotMessage(self, stream, data):
        message = data.message
        if message.content_type == 'application/blink-logging-status':
            if message.content.lower() == "disabled":
                self.remote_party_history = False
                if not self.disable_chat_history:
                    log = NSLocalizedString("Remote chat history disabled", "Label")
                    nc_title = NSLocalizedString("Chat History", "System notification title")
                    nc_subtitle = self.sessionController.titleShort
                    NSApp.delegate().gui_notify(nc_title, log, nc_subtitle)
            else:
                self.remote_party_history = True
                if not self.disable_chat_history:
                    log = NSLocalizedString("Remote chat history enabled", "Label")
                    nc_title = NSLocalizedString("Chat History", "System notification title")
                    nc_subtitle = self.sessionController.titleShort
                    NSApp.delegate().gui_notify(nc_title, log, nc_subtitle)

            self.sessionController.log_info(log)
            self.updateDatabaseRecordingButton()

        elif message.content_type == 'application/blink-icon':
            self.remoteIcon = decode_icon(message.content)
            if self.remoteIcon:
                self.sessionController.log_info('Received remote icon')
                self.chatWindowController.refreshDrawer()

            return

        # render images sent inline
        def filename_generator(name):
            yield name
            from itertools import count
            prefix, extension = os.path.splitext(name)
            for x in count(1):
                yield "%s-%d%s" % (prefix, x, extension)
    
        if message.content_type.startswith("image/"):
            try:
                file_extension = message.content_type.split("/")[1]
            except IndexError:
                pass
            else:
                download_folder = unicodedata.normalize('NFC', NSSearchPathForDirectoriesInDomains(NSDownloadsDirectory, NSUserDomainMask, True)[0])
                file_base = 'blink-inline-image'
                for file_name in filename_generator(os.path.join(download_folder, file_base)):
                    if not os.path.exists(file_name) and not os.path.exists(file_name + "." + file_extension):
                        file_path = file_name + "." + file_extension
                        break

                try:
                    data = base64.b64decode(message.content.encode())
                    self.sessionController.log_info('Image %s received inline' % file_path)
                    fd = open(file_path, "wb+")
                    fd.write(data)
                    fd.close()
                    
                    image = NSImage.alloc().initWithContentsOfFile_(file_path)
                    if image:
                        try:
                            w = image.size().width
                            width = w if w and w < 600 else '100%'
                        except Exception:
                            width = '100%'

                        content = '''<img src="data:%s;base64,%s" border=0 width=%s>''' % (message.content_type, message.content, width)

                        sender_identity = self.sessionController.titleLong
                        icon = NSApp.delegate().contactsWindowController.iconPathForURI(self.sessionController.remoteAOR)
                        
                        self.chatViewController.showMessage(self.sessionController.call_id, str(uuid.uuid1()), 'incoming', sender_identity, icon, content, ISOTimestamp.now(), state="delivered", history_entry=True, is_html=True, media_type='chat')
                        
                        nc_title = NSLocalizedString(sender_identity, "System notification title")
                        nc_subtitle = NSLocalizedString("Sent you an image", "System notification subtitle")
                        nc_body = file_path
                        NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)
                        
                except Exception as e:
                    pass

        if not message.content_type.startswith("text/"):
            return

        hash = hashlib.sha1()
        hash.update((message.content+str(message.timestamp)).encode("utf-8"))
        msgid = hash.hexdigest()

        if msgid not in self.history_msgid_list:
            sender_identity = self.remote_identity or message.sender
            sender = format_identity_to_string(sender_identity, format='compact')
            recipient_identity = message.recipients[0] if message.recipients else self.local_identity
            recipient = format_identity_to_string(recipient_identity, format='compact')
                
            private = data.private
            content = message.content
            #print('Chat got message %s -> %s' % (sender_identity, recipient_identity))
            status = 'delivered'

            if data.encrypted:
                encryption = 'verified' if self.stream.encryption.verified else 'unverified'
            else:
                encryption = ''

            is_html = True if message.content_type == 'text/html' else False
            # save to history
            if 'Welcome to SylkServer!' not in content and 'Received ZRTP Short Authentication String' not in content:
                message = MessageInfo(msgid, direction='incoming', sender=sender_identity, recipient=recipient_identity, timestamp=message.timestamp, content=content, private=private, status="delivered", content_type='html' if is_html else 'text', encryption=encryption)
                self.outgoing_message_handler.add_to_history(message)
                
            if self.chatViewController:
                icon = NSApp.delegate().contactsWindowController.iconPathForURI(self.remote_uri, self.session.remote_focus)
                self.chatViewController.showMessage(self.sessionController.call_id, msgid, 'incoming', sender, icon, content, message.timestamp, is_private=private, recipient=recipient, state=status, is_html=is_html, media_type='chat', encryption=encryption)

            tab = self.chatViewController.outputView.window()
            tab_is_key = tab.isKeyWindow() if tab else False
            tab = None

            # FancyTabViewSwitcher will set unfocused tab item views as Hidden
            if (not tab_is_key or self.chatViewController.view.isHiddenOrHasHiddenAncestor()) and not self.silence_notifications:
                NSApp.requestUserAttention_(NSInformationalRequest)
                nc_title = NSLocalizedString("Chat Message Received", "Window title")
                nc_subtitle = format_identity_to_string(sender_identity, format='full')
                nc_body = html2txt(content.decode('utf-8'))[0:400] if message.content_type == 'text/html' else content[0:400]
                NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)

            NotificationCenter().post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=NotificationData(direction='incoming', history_entry=False, remote_party=self.sessionController.remoteAOR, local_party=format_identity_to_string(self.sessionController.account) if self.sessionController.account is not BonjourAccount() else 'bonjour@local', check_contact=True))

            # disable composing indicator
            if self.remoteTypingTimer:
                self.remoteTypingTimer.invalidate()
                self.remoteTypingTimer = None
            self.chatWindowController.noteSession_isComposing_(self.sessionController, False)

    @objc.python_method
    def _NH_ChatStreamGotComposingIndication(self, stream, data):
        flag = data.state == "active"
        if flag:
            refresh = data.refresh if data.refresh is not None else 120

            if data.last_active is not None and (data.last_active - ISOTimestamp.now() > datetime.timedelta(seconds=refresh)):
                # message is old, discard it
                return

            self.resetIsComposingTimer(refresh)
        else:
            if self.remoteTypingTimer:
                self.remoteTypingTimer.invalidate()
                self.remoteTypingTimer = None

        self.chatWindowController.noteSession_isComposing_(self.sessionController, flag)

    @objc.python_method
    def _NH_ChatStreamDidSetNickname(self, stream, data):
        nickname = self.nickname_request_map.pop(data.message_id)
        self.sessionController.nickname = nickname

    @objc.python_method
    def _NH_ChatStreamDidNotSetNickname(self, stream, data):
        self.nickname_request_map.pop(data.message_id)

    @objc.python_method
    def _NH_BlinkFileTransferDidEnd(self, sender, data):
        if self.sessionController.session is None:
            return
        
        if data.error:
            return

        settings = SIPSimpleSettings()
        if not settings.file_transfer.render_incoming_image_in_chat_window and not settings.file_transfer.render_incoming_video_in_chat_window:
            return

        if self.sessionController.remoteAOR != sender.remote_identity:
            NSApp.delegate().contactsWindowController.showFileTransfers_(None)
            return

        if image_file_extension_pattern.search(data.file_path):
            #content  = NSLocalizedString("Incoming image file transfer finished", "Label")
            try:
                image = NSImage.alloc().initWithContentsOfFile_(data.file_path)
                w = image.size().width
                width = w if w and w < 600 else '100%'
            except Exception:
                width = '100%'

            content = '''<img src="%s" border=0 width=%s>''' % (data.file_path, width)
        else:
            return

        if self.status == STREAM_CONNECTED:
            if sender.direction == 'incoming':
                sender_identity = self.sessionController.titleShort
                icon = NSApp.delegate().contactsWindowController.iconPathForURI(self.sessionController.remoteAOR)
            else:
                sender_identity = None
                icon = NSApp.delegate().contactsWindowController.iconPathForSelf()

            timestamp = ISOTimestamp.now()
            if self.chatViewController:
                self.chatViewController.showMessage(self.sessionController.call_id, str(uuid.uuid1()), sender.direction, sender_identity, icon, content, timestamp, state="delivered", history_entry=True, is_html=True, media_type='chat')

    @objc.python_method
    def _NH_BlinkSessionDidEnd(self, sender, data):
        self.outgoing_message_handler.setDisconnected()
        self.screensharing_handler.setDisconnected()

        self.reset()
        self.chatWindowController.closeDrawer()

    @objc.python_method
    def _NH_BlinkSessionDidFail(self, sender, data):
        reason = data.failure_reason or data.reason
        if reason != 'Session Cancelled':
            if self.last_failure_reason != reason:
                self.last_failure_reason = reason

        if not self.mediastream_ended:
            if reason != 'Session Cancelled':
                if host is None or host.default_ip is None:
                    message = NSLocalizedString("No Internet connection", "Label")
                else:
                    message = reason.title()

                self.showSystemMessage(message, ISOTimestamp.now(), True)

        self.changeStatus(STREAM_IDLE, self.sessionController.endingBy)

        self.outgoing_message_handler.setDisconnected()
        self.screensharing_handler.setDisconnected()
        self.reset()

    @objc.python_method
    def _NH_BlinkSessionDidStart(self, sender, data):
        self.session_succeeded = True
        # toggle collaborative editor to initialize the java script to be able to receive is-composing
        self.last_failure_reason = None
        settings = SIPSimpleSettings()
        if self.sessionController.account is not BonjourAccount() and not settings.chat.disable_collaboration_editor:
            self.toggleEditor()
            self.toggleEditor()

        if self.sessionController.remote_focus:
            self.chatWindowController.drawer.open()


    @objc.python_method
    @run_in_gui_thread
    def _NH_BlinkSessionChangedDisplayName(self, sender, data):
        self.chatWindowController.updateTitle()

    @objc.python_method
    def _NH_BlinkProposalDidFail(self, sender, data):
        if self.last_failure_reason != data.failure_reason:
            message = NSLocalizedString("Proposal failed", "Label")
            self.last_failure_reason = data.failure_reason
            self.showSystemMessage(message, ISOTimestamp.now(), True)

    @objc.python_method
    def _NH_BlinkProposalGotRejected(self, sender, data):
        if data.code != 487:
            if self.last_failure_reason != data.reason:
                self.last_failure_reason = data.reason
                reason = NSLocalizedString("Remote party failed to establish the connection", "Label") if data.reason == 'Internal Server Error' else '%s (%s)' % (data.reason,data.code)
                message = NSLocalizedString("Proposal rejected", "Label") if data.code < 500 else NSLocalizedString("Proposal failed", "Label")
                self.showSystemMessage(message, ISOTimestamp.now(), True)

    @objc.python_method
    def _NH_MediaStreamDidStart(self, sender, data):
        self.chatOtrSmpWindow = ChatOtrSmp(self)
        self.media_started = True
        if self.stream is None or self.stream.msrp is None: # stream may have ended in the mean time
            return
        self.changeStatus(STREAM_CONNECTED)
        self.databaseLoggingButton.setHidden_(not self.history_control_allowed)
        self.sessionController.log_info('Chat stream capabilities: %s' % ", ".join(self.stream.chatroom_capabilities))
        self.sessionController.log_info('Chat stream remote accepted types: %s' % ", ".join(self.stream.remote_accept_types))
        self.sessionController.log_info('Chat stream remote accepted wrapped types: %s' % ", ".join(self.stream.remote_accept_wrapped_types))


        if self.sessionController.remote_focus:
            self.chatWindowController.drawer.open()

        self.last_failure_reason = None
        endpoint = str(self.stream.msrp.full_remote_path[0])
        self.sessionController.log_info("Chat session established to %s" % endpoint)
        self.showSystemMessage(NSLocalizedString("Connection established", "Label"), ISOTimestamp.now())

        # Set nickname if available
        nickname = self.sessionController.nickname
        self.sessionController.nickname = None
        self.setNickname(nickname)

        self.outgoing_message_handler.setConnected(self.stream)

        # needed to set the Audio button state after session has started
        self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self)

        self.sendOwnIcon()
        self.sendLoggingState()
        self.sendZRTPSas()

    @objc.python_method
    def _NH_MediaStreamDidInitialize(self, sender, data):
        self.sessionController.log_info("Chat stream initialized")

    @objc.python_method
    def _NH_MediaStreamDidNotInitialize(self, sender, data):
        if data.reason == 'MSRPRelayAuthError':
            reason = NSLocalizedString("MSRP relay authentication failed", "Label")
        else:
            reason = NSLocalizedString("MSRP connection failed", "Label")

        self.sessionController.log_info(reason)
        self.showSystemMessage(reason, ISOTimestamp.now(), True)
        self.changeStatus(STREAM_FAILED, data.reason)
        self.outgoing_message_handler.setDisconnected()

    @objc.python_method
    def _NH_MediaStreamDidEnd(self, sender, data):
        self.mediastream_ended = True
        self.databaseLoggingButton.setHidden_(True)
        if data.error is not None and 'was closed cleanly' not in data.error:
            self.sessionController.log_info("Chat session failed: %s" % data.error)
            reason = NSLocalizedString("Connection failed", "Label")+ " (%s)" % data.error
            self.showSystemMessage(reason, ISOTimestamp.now(), True)
            self.changeStatus(STREAM_FAILED, data.error)
        else:
            if self.media_started:
                msg = NSLocalizedString("%s left the conversation", "Label") % self.sessionController.titleShort
                self.showSystemMessage(msg, ISOTimestamp.now(), False)
            self.changeStatus(STREAM_IDLE, self.sessionController.endingBy)
            self.sessionController.log_info("Chat session ended")

        self.outgoing_message_handler.setDisconnected()
        self.chatViewController.hideEncryptionFinishedConfirmationDialog()

    @objc.python_method
    def _NH_CFGSettingsObjectDidChange(self, sender, data):
        settings = SIPSimpleSettings()
        if "chat.disable_history" in data.modified:
            if self.sessionController.contact is not None and isinstance(self.sessionController.contact, BlinkPresenceContact) and self.sessionController.contact.contact.disable_chat_history is not None:
                self.disable_chat_history = self.sessionController.contact.contact.disable_chat_history
            else:
                self.disable_chat_history = settings.chat.disable_history
            self.updateDatabaseRecordingButton()
        elif "chat.enable_encryption" in data.modified:
            if self.status == STREAM_CONNECTED:
                if self.is_encrypted and not settings.chat.enable_encryption:
                    self.sessionController.log_info("Chat encryption will stop")
                    self.stream.encryption.stop()
                elif settings.chat.enable_encryption and not self.is_encrypted:
                    self.sessionController.log_info("Chat encryption requested")
                    self.stream.encryption.start()

            self.revalidateToolbar()

    @objc.python_method
    def _NH_ChatReplicationJournalEntryReceived(self, sender, data):
        if self.status == STREAM_CONNECTED:
            return

        data = data.chat_message
        if self.local_uri != data['local_uri'] or self.remote_uri != data['remote_uri']:
            return

        icon = NSApp.delegate().contactsWindowController.iconPathForURI(data['cpim_to'])
        timestamp = ISOTimestamp(data['cpim_timestamp'])
        self.chatViewController.showMessage(data['call_id'], data['msgid'], data['direction'], data['cpim_from'], icon, data['body'], timestamp, is_private=bool(int(data['private'])), recipient=data['cpim_to'], state=data['status'], is_html=True, history_entry=True, media_type='chat', encryption=data['encryption'])

    @objc.python_method
    def resetIsComposingTimer(self, refresh):
        if self.remoteTypingTimer:
            # if we don't get any indications in the request refresh, then we assume remote to be idle
            self.remoteTypingTimer.setFireDate_(NSDate.dateWithTimeIntervalSinceNow_(refresh))
        else:
            self.remoteTypingTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(refresh, self, "remoteBecameIdle:", None, False)

    @objc.python_method
    def end(self, closeTab=False):
        #self.sessionController.log_info("End %s in state %s" % (self, self.status))
        if self.status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self)
            self.changeStatus(STREAM_CANCELLING)
        else:
            if closeTab:
                self.sessionController.endStream(self)
            else:
                # it we have more than chat, we could just stop the chat stream only but is counter intuitive in the GUI so we end the whole session
                self.sessionController.end()
                
            new_state = STATE_IDLE if self.sessionController.state == STATE_IDLE else STREAM_DISCONNECTING
            self.changeStatus(new_state)

    # lifetime of a chat controler: possible deallocation paths
    # 1. User click on close tab: closeTab -> end -> CloseWindow -> deallocTimer -> dealloc
    # 2. User clicks on close window: closeWindow -> for each tab -> closeTab -> end -> CloseWindow -> deallocTimer -> dealloc
    # 3. Session ends by remote: mediaDidEnd -> endStream -> reset -> CloseWindow -> deallocTimer -> dealloc
    # 4. User clicks on disconnect button: end -> reset

    @objc.python_method
    def closeTab(self):
        self.closed = True
        self.sessionController.setVideoConsumer("standalone")

        self.end(True)
        if self.outgoing_message_handler:
            self.outgoing_message_handler.setDisconnected()
        if self.screensharing_handler:
            self.screensharing_handler.setDisconnected()
        self.closeWindow()

        # remove middleware observers
        self.notification_center.discard_observer(self, sender=self.sessionController)
        self.notification_center.discard_observer(self, name='BlinkFileTransferDidEnd')
        self.notification_center.discard_observer(self, name='ChatReplicationJournalEntryReceived')
        self.notification_center.discard_observer(self, name='CFGSettingsObjectDidChange')
        self.notification_center.discard_observer(self, name='BonjourAccountDidAddNeighbour')
        self.notification_center.discard_observer(self, name='BonjourAccountDidUpdateNeighbour')
        self.notification_center.discard_observer(self, name='BonjourAccountDidRemoveNeighbour')


        # remove GUI observers
        NSNotificationCenter.defaultCenter().removeObserver_(self)

        if not self.session_was_active:
            self.notification_center.post_notification("BlinkChatWindowWasClosed", sender=self.sessionController)

        self.startDeallocTimer()

    @objc.python_method
    def reset(self):
        self.mediastream_ended = False
        self.session_succeeded = False
        self.last_failure_reason = None
        self.remoteIcon = None
        self.share_screen_in_conference = False
        self.previous_is_encrypted = False
        self.setScreenSharingToolbarIcon()
        self.resetEditorToolbarIcon()
        self.chatViewController.loadingTextIndicator.setStringValue_("")

        # save chat view so we can print it when session is over
        self.sessionController.chatPrintView = self.chatViewController.outputView

        self.chatWindowController.noteSession_isComposing_(self.sessionController, False)
        self.chatWindowController.noteSession_isScreenSharing_(self.sessionController, False)

    @objc.python_method
    def startDeallocTimer(self):
        self.removeFromSession()
        self.otr_account = None

        if not self.dealloc_timer:
            self.dealloc_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(3.0, self, "deallocTimer:", None, False)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSEventTrackingRunLoopMode)

    def deallocTimer_(self, timer):
        self.release()

    def dealloc(self):
        # dealloc timers
        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
            self.remoteTypingTimer = None

        self.dealloc_timer.invalidate()
        self.dealloc_timer = None

        # release OTR check window
        if self.chatOtrSmpWindow is not None:
            self.chatOtrSmpWindow.close()
        self.chatOtrSmpWindow = None

        # release message handler
        self.outgoing_message_handler.close()

        # release chat view controller
        self.chatViewController.close()

        # release smileys
        self.smileyButton.removeFromSuperview()

        # remove held chat view reference needed for printing
        self.sessionController.chatPrintView = None

        # reset variables
        self.stream = None
        self.outgoing_message_handler = None
        self.chatViewController = None
        self.screensharing_handler = None
        self.history = None
        self.backend = None
        self.notification_center = None

        self.sessionController.log_debug("Dealloc %s" % self)
        self.sessionController = None
        objc.super(ChatController, self).dealloc()


class MessageInfo(object):
    def __init__(self, msgid, direction='outgoing', sender=None, recipient=None, timestamp=None, content=None, private=False, status=None, content_type='text', pending=False, encryption=''):
        self.msgid = msgid
        self.direction = direction
        self.sender = sender
        self.recipient = recipient
        self.timestamp = timestamp
        self.content = content
        self.private = private
        self.status = status
        self.content_type = content_type
        self.pending = pending
        self.encryption = encryption


@implementer(IObserver)
class OutgoingMessageHandler(NSObject):
    """
        Until the stream is connected, all messages typed will be queued and
        marked internally as queued.  Once the stream is connected, queued
        messages will be sent.

        Sent messages are internally marked as unconfirmed. In the UI they are
        marked with Sending...  When a delivery confirmation arrives, they will
        be internally marked as delivered and in the UI the Sending...  will be
        replaced by the delivery timestamp and the messages will be removed from
        the internal queue.  If a failed delivery confirmation is received or no
        confirmation is received before timeout, all unconfirmed messages will
        be marked as undelivered with red in the UI.

        The last undelivered messages will be resent the next time the stream is
        connected.
        """

    stream = None
    connected = False
    delegate = None
    messages = None
    no_report_received_messages = None
    remote_uri = None
    local_uri = None
    must_propose_otr = False

    def initWithView_(self, chatView):
        self = objc.super(OutgoingMessageHandler, self).init()
        if self:
            self.stream = None
            self.connected = None
            self.messages = {}
            self.no_report_received_messages = {}
            self.history = ChatHistory()
            self.delegate = chatView
            self.local_uri = '%s@%s' % (self.delegate.account.id.username, self.delegate.account.id.domain) if self.delegate.account is not BonjourAccount() else 'bonjour@local'
            self.remote_uri = self.delegate.delegate.sessionController.remoteAOR if self.delegate.account is not BonjourAccount() else self.delegate.delegate.sessionController.device_id
        return self

    def dealloc(self):
        self.delegate = None
        objc.super(OutgoingMessageHandler, self).dealloc()

    @objc.python_method
    def close(self):
        self.stream = None
        self.connected = None
        self.history = None

    @objc.python_method
    def _send(self, msgid):
        message = self.messages.pop(msgid)
        message.status = "sent"
        content_type = 'text/html' if message.content_type == 'html' else 'text/plain'
        if message.private and message.recipient is not None:
            try:
                id = self.stream.send_message(message.content, content_type=content_type, timestamp=message.timestamp, recipients=[message.recipient])
                self.no_report_received_messages[msgid] = message
            except ChatStreamError as e:
                self.delegate.sessionController.log_error("Error sending private chat message %s: %s" % (msgid, e))
                self.delegate.markMessage(msgid, MSG_STATE_FAILED, message.private)
                message.status = 'failed'
                self.add_to_history(message)
                return False
        else:
            try:
                newmsg = message.content
                if self.delegate.delegate.stream.encryption.active:
                    if self.delegate.delegate.stream.encryption.verified:
                        message.encryption = 'verified'
                    else:
                        message.encryption = 'unverified'

                    self.delegate.updateEncryptionLock(msgid, message.encryption)

                id = self.stream.send_message(newmsg, timestamp=message.timestamp, content_type=content_type)
                self.no_report_received_messages[msgid] = message

            except ChatStreamError as e:
                self.delegate.sessionController.log_error("Error sending chat message %s: %s" % (msgid, e))
                self.delegate.markMessage(msgid, MSG_STATE_FAILED, message.private)
                message.status = 'failed'
                self.add_to_history(message)
                return False

        self.messages[id] = message

        return id

    @objc.python_method
    def send(self, content, recipient=None, private=False, content_type='text'):
        timestamp = ISOTimestamp.now()
        icon = NSApp.delegate().contactsWindowController.iconPathForSelf()
        recipient_identity = "%s <%s@%s>" % (recipient.display_name, recipient.uri.user.decode(), recipient.uri.host.decode()) if recipient else ''
        sender_identity = self.delegate.account.display_name or self.local_uri

        hash = hashlib.sha1()
        hash.update((content+str(timestamp)).encode("utf-8"))
        msgid = hash.hexdigest()

        self.messages[msgid] = MessageInfo(msgid, sender=self.delegate.account, recipient=recipient, timestamp=timestamp, content=content, private=private, status="queued", content_type=content_type)

        is_html = True if content_type == 'html' else False
        if self.connected:
            try:
                id = self._send(msgid)
            except Exception as e:
                self.delegate.sessionController.log_error("Error sending chat message %s: %s" % (msgid, e))
                self.delegate.showMessage(self.delegate.sessionController.call_id, msgid, 'outgoing', sender_identity, icon, content, timestamp, is_private=private, state="failed", recipient=recipient_identity, is_html=is_html)
            else:
                self.delegate.showMessage(self.delegate.sessionController.call_id, msgid, 'outgoing', sender_identity, icon, content, timestamp, is_private=private, state="sent", recipient=recipient_identity, encryption=self.messages[id].encryption, is_html=is_html)
        else:
            self.messages[msgid].pending=True
            self.delegate.showMessage(self.delegate.sessionController.call_id, msgid, 'outgoing', sender_identity, icon, content, timestamp, is_private=private, state="queued", recipient=recipient_identity, is_html=is_html)

        return True

    @objc.python_method
    def resend(self, msgid, content, recipient=None, private=False, content_type='text'):
        timestamp = ISOTimestamp.now()
        recipient_identity = "%s <%s@%s>" % (recipient.display_name, recipient.uri.user.decode(), recipient.uri.host.decode()) if recipient else ''
        icon = NSApp.delegate().contactsWindowController.iconPathForSelf()

        self.messages[msgid] = MessageInfo(msgid=msgid, recipient=recipient, timestamp=timestamp, content=content, content_type=content_type, private=private, status="queued")

        is_html = True if content_type == 'html' else False
        if self.connected:
            try:
                id = self._send(msgid)
            except Exception as e:
                self.delegate.sessionController.log_error("Error sending chat message %s: %s" % (msgid, e))
                self.delegate.showSystemMessage(self.delegate.sessionController.call_id, NSLocalizedString("Message delivery failure", "Label"), timestamp, True)
            else:
                self.delegate.showMessage(self.delegate.sessionController.call_id, msgid, 'outgoing', None, icon, content, timestamp, is_private=private, state="sent", recipient=recipient_identity, encryption=self.messages[id].encryption, is_html=is_html)
        else:
            self.messages[msgid].pending=True
            self.delegate.showMessage(self.delegate.sessionController.call_id, msgid, 'outgoing', None, icon, content, timestamp, is_private=private, state="queued", recipient=recipient_identity, is_html=is_html)

    @objc.python_method
    def setConnected(self, stream):
        self.stream = stream
        self.no_report_received_messages = {}
        self.connected = True
        self.sendPendingMessages()

        NotificationCenter().add_observer(self, sender=stream)

    @objc.python_method
    def sendPendingMessages(self):
        pending = (msgid for msgid in list(self.messages.keys()) if self.messages[msgid].pending)
        for msgid in pending:
            try:
                private = self.messages[msgid].private
                encryption = self.messages[msgid].encryption
            except KeyError:
                continue
            else:
                sent = self._send(msgid)
                if not sent:
                    self.delegate.sessionController.log_error("Error sending queued message: %s" % msgid)
                else:
                    self.delegate.markMessage(msgid, MSG_STATE_SENDING, private)

    @objc.python_method
    def setDisconnected(self):
        self.connected = False
        pending = (msgid for msgid in list(self.messages.keys()) if self.messages[msgid].pending)
        for msgid in pending:
            try:
                message = self.messages.pop(msgid)
            except KeyError:
                pass
            else:
                message.status = 'failed'
                self.delegate.sessionController.log_error("Error sending chat message %s" % msgid)
                self.delegate.markMessage(msgid, MSG_STATE_FAILED)
                self.add_to_history(message)

        self.messages = {}
        for msgid in list(self.no_report_received_messages.keys()):
            try:
                message = self.no_report_received_messages.pop(msgid)
            except KeyError:
                pass
            else:
                self.delegate.sessionController.log_error("No delivery report received for chat message %s" % msgid)
                self.delegate.markMessage(msgid, MSG_STATE_FAILED)
                self.add_to_history(message)

        if self.stream:
            NotificationCenter().discard_observer(self, sender=self.stream)
            self.stream = None

    @objc.python_method
    def markMessage(self, message, state):
        message.state = state
        self.delegate.markMessage(message.msgid, state, message.private)

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    @objc.python_method
    def _NH_ChatStreamDidDeliverMessage(self, sender, data):
        try:
            message = self.messages.pop(data.message_id)
            if message:
                try:
                    del self.no_report_received_messages[message.msgid]
                except KeyError:
                    pass
                if message.status != 'failed':
                    # this can happen if we highjacked the conection with OTR, and we sent a bogus message out if encryption failed
                    message.status='delivered'
                    self.markMessage(message, MSG_STATE_DELIVERED)
                else:
                    self.markMessage(message, MSG_STATE_FAILED)
                self.add_to_history(message)
                self.lastDeliveredTime = time.time()
        except KeyError:
            pass

    @objc.python_method
    def _NH_ChatStreamDidNotDeliverMessage(self, sender, data):
        self.delegate.sessionController.log_error("Chat message %s was not delivered: %s" % (data.message_id, data.reason))
        try:
            message = self.messages.pop(data.message_id)
            if message:
                try:
                    del self.no_report_received_messages[message.msgid]
                except KeyError:
                    pass

                message.status = 'failed'
                self.delegate.sessionController.log_error("Chat message %s was not delivered" % message.msgid)
                self.markMessage(message, MSG_STATE_FAILED)
                self.add_to_history(message)
        except KeyError:
            pass

    @objc.python_method
    def add_to_history(self, message):
        if self.delegate.delegate.disable_chat_history:
            return

        if not self.delegate.delegate.remote_party_history:
            return

        # writes the record to the sql database
        cpim_to = "%s <%s@%s>" % (message.recipient.display_name, message.recipient.uri.user.decode(), message.recipient.uri.host.decode()) if message.recipient else ''
        cpim_from = format_identity_to_string(message.sender, format='full') if message.sender else ''
        cpim_timestamp = str(message.timestamp)
        private = "1" if message.private else "0"
        self.history.add_message(message.msgid, 'chat', self.local_uri, self.remote_uri, message.direction, cpim_from, cpim_to, cpim_timestamp, message.content, message.content_type, private, message.status, call_id=self.delegate.sessionController.call_id, encryption=message.encryption)


@implementer(IObserver)
class ConferenceScreenSharingHandler(object):

    delegate = None
    connected = False
    screenSharingTimer = None
    stream = None
    rect = None
    frames = 0.0
    last_time = None
    current_framerate = None
    log_first_frame = False
    show_preview = False
    may_send = True # wait until previous screen has been sent
    framerate = 1
    width = 1024
    window_id = None
    compression = 0.7 # jpeg compression
    quality = 'medium'
    quality_settings = {'low':    {'compression': 0.3, 'width': 800,  'max_width': None, 'framerate': 1},
                        'medium': {'compression': 0.5, 'width': 1024, 'max_width': None, 'framerate': 1},
                        'high':   {'compression': 0.7, 'width': None, 'max_width': 1680,'framerate': 1}
    }

    def setDelegate(self, delegate):
        self.delegate = delegate

    def setQuality(self, quality):
        if quality:
            self.quality = quality
        else:
            self.quality = 'medium'
        BlinkLogger().log_info('Set screen sharing quality to %s' % self.quality)
        self.compression = self.quality_settings[self.quality]['compression']
        self.width = self.quality_settings[self.quality]['width']
        self.max_width = self.quality_settings[self.quality]['max_width']
        self.framerate = self.quality_settings[self.quality]['framerate']
        self.log_first_frame = True
        NSUserDefaults.standardUserDefaults().setValue_forKey_(self.quality, "ScreensharingQuality")

    def setShowPreview(self):
        self.show_preview = True

    def setWindowId(self, id):
        self.window_id = id
        self.show_preview = True

    def setConnected(self, stream):
        self.log_first_frame = True
        self.connected = True
        self.stream = stream
        quality = NSUserDefaults.standardUserDefaults().stringForKey_("ScreensharingQuality")
        self.setQuality(quality)
        self.last_time = time.time()
        self.show_preview = True
        NotificationCenter().add_observer(self, sender=stream)

        if self.screenSharingTimer is None:
            self.screenSharingTimer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.1, self, "sendScreenshotTimer:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.screenSharingTimer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.screenSharingTimer, NSEventTrackingRunLoopMode)
    # use UITrackingRunLoopMode in iOS instead of NSEventTrackingRunLoopMode

    def setDisconnected(self):
        self.log_first_frame = False
        self.connected = False
        self.may_send = True
        self.frames = 0
        self.last_time = None
        self.show_preview = False

        if self.screenSharingTimer is not None:
            self.screenSharingTimer.invalidate()
            self.screenSharingTimer = None

        if self.stream:
            NotificationCenter().discard_observer(self, sender=self.stream)
            self.stream = None

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_ChatStreamDidDeliverMessage(self, sender, data):
        self.may_send = True
        self.last_snapshot_time = time.time()

    def _NH_ChatStreamDidNotDeliverMessage(self, sender, data):
        self.may_send = True
        self.last_snapshot_time = time.time()

    @allocate_autorelease_pool
    def sendScreenshotTimer_(self, timer):
        def screenSharingWindowExists(id):
            listOptions = kCGWindowListExcludeDesktopElements
            windowList = CGWindowListCopyWindowInfo(listOptions, kCGNullWindowID)
            i = 0
            while i < windowList.count():
                wob = windowList.objectAtIndex_(i)
                if wob.objectForKey_(kCGWindowNumber) == id:
                    del windowList
                    return True
                i += 1
            return False

        dt = time.time() - self.last_time
        if dt >= 1:
            self.current_framerate = self.frames / dt
            self.frames = 0.0
            self.last_time = time.time()

        if self.may_send and dt >= 1/self.framerate:
            self.frames = self.frames + 1
            rect = CGDisplayBounds(CGMainDisplayID())
            if self.window_id:
                if screenSharingWindowExists(self.window_id):
                    image = CGWindowListCreateImage(rect, kCGWindowListOptionIncludingWindow, self.window_id, kCGWindowImageBoundsIgnoreFraming)
                else:
                    self.window_id = None
                    if self.delegate:
                        self.delegate.toggleScreensharingWithConferenceParticipants()
                    else:
                        self.setDisconnected()
                    return
            else:
                image = CGWindowListCreateImage(rect, kCGWindowListOptionOnScreenOnly, kCGNullWindowID, kCGWindowImageDefault)
            if CGImageGetWidth(image) <= 1:
                return
            image = NSImage.alloc().initWithCGImage_size_(image, NSZeroSize)
            originalSize = image.size()
            if self.width is not None and originalSize.width > self.width:
                resizeWidth = self.width
                resizeHeight = self.width * originalSize.height/originalSize.width
                scaled_image = NSImage.alloc().initWithSize_(NSMakeSize(resizeWidth, resizeHeight))
                scaled_image.lockFocus()
                image.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, resizeWidth, resizeHeight), NSMakeRect(0, 0, originalSize.width, originalSize.height), NSCompositeSourceOver, 1.0)
                scaled_image.unlockFocus()
                image = scaled_image

            if self.width is None and self.max_width is not None and originalSize.width > self.max_width:
                resizeWidth = self.max_width
                resizeHeight = self.max_width * originalSize.height/originalSize.width
                scaled_image = NSImage.alloc().initWithSize_(NSMakeSize(resizeWidth, resizeHeight))
                scaled_image.lockFocus()
                image.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, resizeWidth, resizeHeight), NSMakeRect(0, 0, originalSize.width, originalSize.height), NSCompositeSourceOver, 1.0)
                scaled_image.unlockFocus()
                image = scaled_image

            if self.show_preview:
                ScreensharingPreviewPanel(image)
                self.show_preview = False

            jpg_data = NSBitmapImageRep.alloc().initWithData_(image.TIFFRepresentation()).representationUsingType_properties_(NSJPEGFileType, {NSImageCompressionFactor: self.compression})
            # this also works and produces the same result, but it's not documented anywhere
            #jpeg = image.IKIPJPEGDataWithMaxSize_compression_(image.size().width, self.compression)

            data = base64.b64encode(jpg_data.bytes().tobytes()).decode()

            if self.log_first_frame:
                self.delegate.sessionController.log_info('Sending %s bytes with %dx%d screen' % (format_size(len(data)), image.size().width, image.size().height))
                self.log_first_frame = False
            self.delegate.sessionController.log_debug('Sending %s bytes with %dx%d screen' % (format_size(len(data)), image.size().width, image.size().height))
            self.may_send = False
            if self.stream:
                self.stream.send_message(data, content_type='application/blink-screensharing', timestamp=ISOTimestamp.now())

