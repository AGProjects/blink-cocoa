# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSCompositeSourceOver,
                    NSEventTrackingRunLoopMode,
                    NSFontAttributeName,
                    NSImageCompressionFactor,
                    NSInformationalRequest,
                    NSJPEGFileType,
                    NSSplitViewDidResizeSubviewsNotification,
                    NSSplitViewDividerStyleThick,
                    NSSplitViewDividerStyleThin,
                    NSToolbarPrintItemIdentifier,
                    NSWindowBelow)
from Foundation import (NSAttributedString,
                        NSBitmapImageRep,
                        NSBundle,
                        NSColor,
                        NSDate,
                        NSDictionary,
                        NSFont,
                        NSImage,
                        NSMakeRect,
                        NSMakeSize,
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
                        NSWorkspace)
from Quartz import (CGDisplayBounds,
                    CGImageGetWidth,
                    CGMainDisplayID,
                    CGWindowListCopyWindowInfo,
                    CGWindowListCreateImage,
                    kCGWindowImageBoundsIgnoreFraming,
                    kCGWindowListExcludeDesktopElements,
                    kCGWindowListOptionIncludingWindow,
                    kCGWindowNumber)
import objc

import datetime
import hashlib
import os
import time
import unicodedata
import uuid
import potr
import potr.crypt
import potr.context

from dateutil.tz import tzlocal
from gnutls.errors import GNUTLSError

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from itertools import chain
from zope.interface import implements

from sipsimple.account import BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams import ChatStream, ChatStreamError
from sipsimple.streams.applications.chat import CPIMIdentity
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp

import ChatWindowController
from BlinkLogger import BlinkLogger
from ChatViewController import ChatViewController, MSG_STATE_FAILED, MSG_STATE_SENDING, MSG_STATE_DELIVERED
from ChatOTR import BlinkOtrAccount, ChatOtrSmp
from ContactListModel import encode_icon, decode_icon
from VideoView import VideoView
from FileTransferWindowController import openFileTransferSelectionDialog
from HistoryManager import ChatHistory
from MediaStream import MediaStream, STATE_IDLE, STREAM_IDLE, STREAM_FAILED, STREAM_CONNECTED, STREAM_PROPOSING, STREAM_WAITING_DNS_LOOKUP, STREAM_INCOMING, STREAM_CONNECTING, STREAM_RINGING, STREAM_DISCONNECTING, STREAM_CANCELLING
from MediaStream import STATE_IDLE
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
objc.loadBundleFunctions(bundle, globals(), (('SetSystemUIMode', 'III', " Sets the presentation mode for system-provided user interface elements."),))


kCGWindowListOptionOnScreenOnly = 1 << 0
kCGNullWindowID = 0
kCGWindowImageDefault = 0


class ChatController(MediaStream):
    implements(IObserver)

    chatViewController = objc.IBOutlet()
    smileyButton = objc.IBOutlet()

    splitView = objc.IBOutlet()
    splitViewFrame = None
    video_frame_visible = False

    videoContainer = objc.IBOutlet()
    inputContainer = objc.IBOutlet()
    outputContainer = objc.IBOutlet()
    databaseLoggingButton = objc.IBOutlet()
    privateLabel = objc.IBOutlet()

    fullScreenVideoPanel = objc.IBOutlet()
    fullScreenVideoPanelToobar = objc.IBOutlet()

    document = None
    fail_reason = None
    sessionController = None
    stream = None
    finishedLoading = False
    showHistoryEntries = 50
    mustShowUnreadMessages = False

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
    new_fingerprints = {}
    otr_account = None
    chatOtrSmpWindow = None
    disable_chat_history = False
    remote_party_history = True

    @classmethod
    def createStream(self):
        return ChatStream()

    def initWithOwner_stream_(self, sessionController, stream):
        self = super(ChatController, self).initWithOwner_stream_(sessionController, stream)
        BlinkLogger().log_debug(u"Creating %s" % self)
        self.mediastream_failed = False
        self.mediastream_ended = False
        self.mediastream_started = False
        self.session_succeeded = False
        self.last_failure_reason = None
        self.remoteIcon = None
        self.share_screen_in_conference = False

        self.previous_is_encrypted = False
        self.history_msgid_list=set()

        self.remote_uri = format_identity_to_string(self.sessionController.remotePartyObject)
        self.local_uri = '%s@%s' % (self.sessionController.account.id.username, self.sessionController.account.id.domain) if self.sessionController.account is not BonjourAccount() else 'bonjour'

        self.require_encryption = self.sessionController.contact.contact.require_encryption if self.sessionController.contact is not None else True
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, name='BlinkFileTransferDidEnd')
        self.notification_center.add_observer(self, name='BlinkMuteChangedState')
        self.notification_center.add_observer(self, name='ChatReplicationJournalEntryReceived')
        self.notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        self.notification_center.add_observer(self, name='OTRPrivateKeyDidChange')

        NSBundle.loadNibNamed_owner_("ChatView", self)

        self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("ChatView", "html"))
        if self.sessionController.account is BonjourAccount():
            self.chatViewController.setHandleScrolling_(False)
            self.chatViewController.lastMessagesLabel.setHidden_(True)

        if self.sessionController.contact is not None and self.sessionController.contact.contact.disable_smileys:
            self.chatViewController.expandSmileys = False
            self.chatViewController.toggleSmileys(self.chatViewController.expandSmileys)

        self.chatViewController.setAccount_(self.sessionController.account)
        self.chatViewController.resetRenderedMessages()

        self.outgoing_message_handler = OutgoingMessageHandler.alloc().initWithView_(self.chatViewController)

        self.screensharing_handler = ConferenceScreenSharingHandler()
        self.screensharing_handler.setDelegate(self)

        self.history = ChatHistory()
        self.backend = SIPManager()
        self.chatOtrSmpWindow = ChatOtrSmp(self)
        self.init_otr()

        if self.sessionController.contact is not None and self.sessionController.contact.contact.disable_chat_history is not None:
            self.disable_chat_history = self.sessionController.contact.contact.disable_chat_history
        else:
            settings = SIPSimpleSettings()
            self.disable_chat_history = settings.chat.disable_history

        self.updateDatabaseRecordingButton()

        return self

    def updateDatabaseRecordingButton(self):
        settings = SIPSimpleSettings()
        remote = format_identity_to_string(self.sessionController.remotePartyObject, format='full')

        if self.remote_party_history and not self.disable_chat_history:
            self.privateLabel.setHidden_(True)
            self.databaseLoggingButton.setImage_(NSImage.imageNamed_("database-on"))
            self.databaseLoggingButton.setToolTip_("Text conversation is saved to history database")
        elif not self.remote_party_history and not self.disable_chat_history:
            self.databaseLoggingButton.setImage_(NSImage.imageNamed_("database-remote-off"))
            self.privateLabel.setHidden_(False)
            self.databaseLoggingButton.setToolTip_(NSLocalizedString("%s wishes that text conversation is not saved in history database" % remote, "Tooltip text"))
        else:
            self.privateLabel.setHidden_(False)
            self.databaseLoggingButton.setImage_(NSImage.imageNamed_("database-local-off"))
            self.databaseLoggingButton.setToolTip_("Text conversation is not saved to history database")

    def init_otr(self, disable_encryption=False):
        from ChatOTR import DEFAULT_OTR_FLAGS
        peer_options = DEFAULT_OTR_FLAGS
        if disable_encryption:
            peer_options['REQUIRE_ENCRYPTION'] = False
            peer_options['ALLOW_V2'] = False
            peer_options['SEND_TAG'] = False
            peer_options['WHITESPACE_START_AKE'] = False
            peer_options['ERROR_START_AKE'] = False
        else:
            if self.sessionController.contact is not None:
                peer_options['REQUIRE_ENCRYPTION'] = self.sessionController.contact.contact.require_encryption
            settings = SIPSimpleSettings()
            peer_options['ALLOW_V2'] = settings.chat.enable_encryption

        self.otr_account = BlinkOtrAccount(peer_options=peer_options)
        self.otr_account.loadTrusts()

    def setEncryptionState(self, ctx):
        if self.previous_is_encrypted != self.is_encrypted:
            self.previous_is_encrypted = self.is_encrypted
            fingerprint = str(ctx.getCurrentKey())
            self.sessionController.log_info('Remote OTR fingerprint %s' %fingerprint)
        self.updateEncryptionWidgets()

    @property
    def otr_status(self):
        ctx = self.otr_account.getContext(self.sessionController.call_id)
        finished = ctx.state == potr.context.STATE_FINISHED
        encrypted = finished or ctx.state == potr.context.STATE_ENCRYPTED
        trusted = encrypted and bool(ctx.getCurrentTrust())
        return (encrypted, trusted, finished)

    @property
    def is_encrypted(self):
        return self.otr_status[0]

    @property
    def screensharing_allowed(self):
        return 'com.ag-projects.screen-sharing' in chain(*(attr.split() for attr in self.stream.remote_media.attributes.getall('chatroom')))

    @property
    def control_allowed(self):
        return 'com.ag-projects.sylkserver-control' in chain(*(attr.split() for attr in self.stream.remote_media.attributes.getall('chatroom')))


    @property
    def chatWindowController(self):
        return NSApp.delegate().contactsWindowController.chatWindowController

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

    def saveSplitterPosition(self):
        self.mainViewSplitterPosition={'output_frame': self.outputContainer.frame(), 'input_frame': self.inputContainer.frame()}
        if self.videoContainer:
            self.mainViewSplitterPosition['video_frame']=self.videoContainer.frame()

    def restoreSplitterPosition(self):
        if self.mainViewSplitterPosition:
            if self.videoContainer and self.mainViewSplitterPosition['video_frame']:
                self.videoContainer.setFrame_(self.mainViewSplitterPosition['video_frame'])
            self.outputContainer.setFrame_(self.mainViewSplitterPosition['output_frame'])
            self.inputContainer.setFrame_(self.mainViewSplitterPosition['input_frame'])

    def getContentView(self):
        return self.chatViewController.view

    def showSystemMessage(self, message, timestamp, is_error=False):
        if self.chatViewController:
            self.chatViewController.showSystemMessage(self.sessionController.call_id, message, timestamp, is_error)

    def insertSmiley_(self, sender):
        smiley = sender.representedObject()
        self.chatViewController.appendAttributedString_(smiley)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def changeStatus(self, newstate, fail_reason=None):
        self.status = newstate
        MediaStream.changeStatus(self, newstate, fail_reason)

    def openChatWindow(self):
        if self.chatWindowController is None:
            NSApp.delegate().contactsWindowController.chatWindowController = ChatWindowController.ChatWindowController.alloc().init()

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

    def closeWindow(self):
        self.chatWindowController.removeSession_(self.sessionController)
        if not self.chatWindowController.sessions:
            self.chatWindowController.window().orderOut_(None)

    def startOutgoing(self, is_update):
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

    def startIncoming(self, is_update):
        self.session_succeeded = False
        self.last_failure_reason = None
        self.notification_center.add_observer(self, sender=self.stream)
        self.notification_center.add_observer(self, sender=self.sessionController)
        self.session_was_active = True
        self.mustShowUnreadMessages = True
        self.openChatWindow()
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_INCOMING)

    def sendFiles(self, fnames):
        filenames = [unicodedata.normalize('NFC', file) for file in fnames if os.path.isfile(file)]
        if filenames:
            self.sessionControllersManager.send_files_to_contact(self.sessionController.account, self.sessionController.target_uri, filenames)
            return True
        return False

    def sendOwnIcon(self):
        if self.stream and not self.sessionController.session.remote_focus:
            base64icon = encode_icon(self.chatWindowController.own_icon)
            self.stream.send_message(str(base64icon), content_type='application/blink-icon', timestamp=ISOTimestamp.now())

    def sendLoggingState(self):
        if self.status == STREAM_CONNECTED:
            text = 'enabled' if not self.disable_chat_history else 'disabled'
            self.stream.send_message(text, content_type='application/blink-logging-status', timestamp=ISOTimestamp.now())

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
        if self.sessionController.contact is not None:
            self.sessionController.contact.contact.disable_chat_history = self.disable_chat_history
            self.sessionController.contact.contact.save()
        self.updateDatabaseRecordingButton()
        self.sendLoggingState()

    def userClickedEncryptionMenu_(self, sender):
        tag = sender.tag()
        ctx = self.otr_account.getContext(self.sessionController.call_id)
        if tag == 3: # required
            self.require_encryption = not self.require_encryption
            if self.sessionController.contact is not None:
                self.sessionController.contact.contact.require_encryption = self.require_encryption
                self.sessionController.contact.contact.save()

            self.otr_account.peer_options['REQUIRE_ENCRYPTION'] = self.require_encryption
            if self.status == STREAM_CONNECTED and self.require_encryption and not self.sessionController.remote_focus:
                self.outgoing_message_handler.propose_otr()

        elif tag == 4: # active
            if self.status == STREAM_CONNECTED and self.is_encrypted:
                ctx.disconnect()
            elif not self.sessionController.remote_focus:
                self.outgoing_message_handler.propose_otr()
        elif tag == 5: # verified
            fingerprint = ctx.getCurrentKey()
            if fingerprint:
                otr_fingerprint_verified = self.otr_account.getTrust(self.sessionController.remoteSIPAddress, str(fingerprint))
                if otr_fingerprint_verified:
                    self.otr_account.removeFingerprint(self.sessionController.remoteSIPAddress, str(fingerprint))
                else:
                    self.otr_account.setTrust(self.sessionController.remoteSIPAddress, str(fingerprint), 'verified')

        elif tag == 9: # SMP window
            self.chatOtrSmpWindow.show()

        elif tag == 10:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://www.cypherpunks.ca/otr/Protocol-v2-3.1.0.html"))

        self.chatWindowController.revalidateToolbar()

    @objc.IBAction
    def userClickedFullScreenToolbarButton_(self, sender):
        if sender.itemIdentifier() == 'hangup':
            self.closeTab()
        elif sender.itemIdentifier() == 'mirror':
            self.toggleVideoMirror()
        elif sender.itemIdentifier() == 'mute':
            self.backend.mute(False if self.backend.is_muted() else True)
            self.notification_center.post_notification("BlinkMuteChangedState", sender=self)
        elif sender.itemIdentifier() == 'hold':
            if self.sessionController.hasStreamOfType("audio"):
                audio_stream = self.sessionController.streamHandlerOfType("audio")
                if self.status == STREAM_CONNECTED and not self.sessionController.inProposal:
                    if audio_stream.holdByLocal:
                        audio_stream.unhold()
                        audio_stream.view.setSelected_(True)
                        sender.setImage_(NSImage.imageNamed_("pause"))
                    else:
                        sender.setImage_(NSImage.imageNamed_("paused"))
                        audio_stream.hold()
        elif sender.itemIdentifier() == 'participants':
            self.chatWindowController.window().performZoom_(None)
        elif sender.itemIdentifier() == 'exit':
            self.exitFullScreen()

    def textView_doCommandBySelector_(self, textView, selector):
        if selector == "insertNewline:" and self.chatViewController.inputText == textView:
            original = textView.string()
            text = unicode(original)
            textView.setString_("")
            if text:
                recipient = '%s <sip:%s>' % (self.sessionController.contactDisplayName, format_identity_to_string(self.sessionController.remotePartyObject))
                try:
                    identity = CPIMIdentity.parse(recipient)
                except ValueError:
                    identity = None
                if not self.outgoing_message_handler.send(text, recipient=identity):
                    textView.setString_(original)
                else:
                    NotificationCenter().post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=NotificationData(direction='outgoing', history_entry=False, remote_party=format_identity_to_string(self.sessionController.remotePartyObject, format='full'), local_party=format_identity_to_string(self.sessionController.account) if self.sessionController.account is not BonjourAccount() else 'bonjour', check_contact=True))

            if not self.stream or self.status in [STREAM_FAILED, STREAM_IDLE]:
                self.sessionController.log_info(u"Session not established, starting it")
                if self.outgoing_message_handler.messages:
                    # save unsend messages and pass them to the newly spawned handler
                    self.sessionController.pending_chat_messages = self.outgoing_message_handler.messages
                self.sessionController.startChatSession()
            self.chatViewController.resetTyping()
            return True
        return False

    def chatView_becameIdle_(self, chatView, time):
        if self.stream:
            self.stream.send_composing_indication("idle", 60, last_active=time)

    def chatView_becameActive_(self, chatView, time):
        if self.stream:
            self.stream.send_composing_indication("active", 60, last_active=time)
            if self.outgoing_message_handler.must_propose_otr:
                self.outgoing_message_handler.propose_otr()


    def chatViewDidLoad_(self, chatView):
         self.replay_history()

    def updateToolbarMuteIcon(self):
        if self.fullScreenVideoPanel:
            try:
                mute_item = (item for item in self.fullScreenVideoPanelToobar.visibleItems() if item.itemIdentifier()=='mute').next()
            except StopIteration:
                pass
            else:
                if self.backend.is_muted():
                    mute_item.setImage_(NSImage.imageNamed_("muted"))
                else:
                    mute_item.setImage_(NSImage.imageNamed_("mute"))

    def fullScreenViewPressedEscape(self):
        self.exitFullScreen()

    def enterFullScreen(self):
        self.chatWindowController.drawer.open()

        self.splitViewFrame = self.chatWindowController.window().frame()

        self.saveSplitterPosition()

        self.splitView.setDividerStyle_(NSSplitViewDividerStyleThin)
        output_frame = self.outputContainer.frame()
        output_frame.size.height = 0
        self.outputContainer.setFrame_(output_frame)
        input_frame = self.outputContainer.frame()
        input_frame.size.height = 0
        self.inputContainer.setFrame_(input_frame)

        # Hide Dock and other screen items
        SetSystemUIMode(kUIModeAllHidden, 0)

        self.chatWindowController.window().makeFirstResponder_(self.videoContainer)

        self.chatWindowController.window().setMovableByWindowBackground_(True)
        fullframe = NSScreen.mainScreen().frame()
        fullframe.size.height += 20
        self.chatWindowController.window().setFrame_display_animate_(fullframe, True, True)
        self.chatWindowController.window().setMovable_(False)

        self.notification_center.post_notification("BlinkVideoEnteredFullScreen", sender=self)

        self.showFullScreenVideoPanel()
        self.showVideoMirror()

        self.chatWindowController.window().setInitialFirstResponder_(self.videoContainer)

    def exitFullScreen(self):
        self.hideVideoMirror()

        if self.splitViewFrame:
            self.chatWindowController.window().setFrame_display_(self.splitViewFrame, True)
            self.chatWindowController.window().setMovable_(True)

            self.splitView.setDividerStyle_(NSSplitViewDividerStyleThin)
            self.restoreSplitterPosition()
            self.splitViewFrame = None

        if self.fullScreenVideoPanel:
            self.fullScreenVideoPanel.orderOut_(self)

        # Restore Dock and other screen items
        SetSystemUIMode(kUIModeNormal, kUIOptionAutoShowMenuBar)

        self.notification_center.post_notification("BlinkVideoExitedFullScreen", sender=self)

    def showFullScreenVideoPanel(self):
        if not self.fullScreenVideoPanel:
            NSBundle.loadNibNamed_owner_("FullScreenVideoPanel", self)

            userdef = NSUserDefaults.standardUserDefaults()
            savedFrame = userdef.stringForKey_("NSWindow Frame FullScreenVideoPanel")

            if savedFrame:
                x, y, w, h = str(savedFrame).split()[:4]
                frame = NSMakeRect(int(x), int(y), int(w), int(h))
                self.fullScreenVideoPanel.setFrame_display_(frame, True)

        self.fullScreenVideoPanel.orderFront_(None)
        self.fullScreenVideoPanelToobar.validateVisibleItems()
        self.updateToolbarMuteIcon()

    def showVideoMirror(self):
        NSApp.delegate().contactsWindowController.showVideoMirrorWindow()

    def hideVideoMirror(self):
        NSApp.delegate().contactsWindowController.hideVideoMirrorWindow()

    def toggleVideoMirror(self):
        if NSApp.delegate().contactsWindowController.mirrorWindow.visible:
            self.hideVideoMirror()
        else:
            self.showVideoMirror()

    def showChatViewWhileVideoActive(self):
        if self.video_frame_visible:
            view_height = self.splitView.frame().size.height
            input_frame = self.inputContainer.frame()
            output_frame = self.outputContainer.frame()

            splitter_height = 5
            new_output_height = 200
            new_input_height = 35

            self.splitView.setDividerStyle_(NSSplitViewDividerStyleThin)

            # video frame
            video_height = view_height - input_frame.size.height - 2 * splitter_height - new_output_height
            video_frame = NSMakeRect(0, 0, input_frame.size.width, video_height)
            self.videoContainer.setFrame_(video_frame)

            # output frame
            output_frame.size.height = new_output_height
            self.outputContainer.setFrame_(output_frame)

            # input frame
            input_frame.size.height = new_input_height
            self.inputContainer.setFrame_(input_frame)

    def showChatViewWithEditorWhileVideoActive(self):
        if self.video_frame_visible:

            splitter_height = 5
            new_input_height = 35

            new_output_height = 300 if self.chatViewController.editorVisible else 0

            view_height = self.splitView.frame().size.height
            output_frame = self.outputContainer.frame()
            input_frame = self.inputContainer.frame()

            self.splitView.setDividerStyle_(NSSplitViewDividerStyleThin)

            # video frame
            video_height = view_height - input_frame.size.height - 2 * splitter_height - new_output_height
            video_frame = NSMakeRect(0, 0, input_frame.size.width, video_height)
            self.videoContainer.setFrame_(video_frame)

            # output frame
            output_frame.size.height = new_output_height
            self.outputContainer.setFrame_(output_frame)

            # input frame
            input_frame.size.height = new_input_height
            self.inputContainer.setFrame_(input_frame)


    def isOutputFrameVisible(self):
        return True if self.outputContainer.frame().size.height > 10 else False

    def toggleVideoFrame(self):
        input_frame = self.inputContainer.frame()
        output_frame = self.outputContainer.frame()

        view_height = self.splitView.frame().size.height

        if not self.video_frame_visible:
            #window.drawer.close()

            splitter_height = 5
            self.splitView.setDividerStyle_(NSSplitViewDividerStyleThin)

            self.splitView.addSubview_positioned_relativeTo_(self.videoContainer,  NSWindowBelow, self.outputContainer)
            self.videoContainer.setDelegate_(self)
            self.videoContainer.showVideo()

            # input frame
            input_frame.size.height = 35
            self.inputContainer.setFrame_(input_frame)

            # video frame
            video_height = view_height - input_frame.size.height - 2 * splitter_height
            video_frame = NSMakeRect(0, 0, input_frame.size.width, video_height)
            self.videoContainer.setFrame_(video_frame)

            # output frame
            output_frame.size.height = 0
            self.outputContainer.setFrame_(output_frame)

            self.chatViewController.searchMessagesBox.setHidden_(True)
            self.chatViewController.lastMessagesLabel.setHidden_(True)
            self.chatViewController.showRelatedMessagesButton.setHidden_(True)

            self.video_frame_visible = True

        else:
            self.chatWindowController.drawer.open()
            self.splitView.setDividerStyle_(NSSplitViewDividerStyleThick)
            splitter_height = 10
            self.videoContainer.hideVideo()
            self.videoContainer.removeFromSuperview()
            self.videoContainer.setDelegate_(None)

            # input frame
            input_frame.size.height = 65
            self.inputContainer.setFrame_(input_frame)

            # output frame
            output_frame.size.height = view_height - input_frame.size.height - splitter_height
            self.outputContainer.setFrame_(output_frame)

            # output view frame
            search_box_height = 27

            output_view_frame = self.chatViewController.outputView.frame()
            output_view_frame.size.height = output_frame.size.height - search_box_height
            self.chatViewController.outputView.setFrame_(output_view_frame)

            self.chatViewController.searchMessagesBox.setHidden_(False)
            self.chatViewController.lastMessagesLabel.setHidden_(False)
            self.chatViewController.showRelatedMessagesButton.setHidden_(not self.chatViewController.related_messages)

            self.video_frame_visible = False

    def scroll_back_in_time(self):
        try:
            msgid = self.history_msgid_list[0]
        except IndexError:
            msgid = None

        self.chatViewController.clear()
        self.chatViewController.resetRenderedMessages()
        self.replay_history(msgid)

    @run_in_green_thread
    @allocate_autorelease_pool
    def replay_history(self, scrollToMessageId=None):
        if not self:
            return

        blink_contact = self.sessionController.contact
        if not blink_contact:
            remote_uris = self.remote_uri
        else:
            remote_uris = list(str(uri.uri) for uri in blink_contact.uris if '@' in uri.uri)

        if self.sessionController.account is not BonjourAccount():
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
                    self.zoom_period_label = 'Displaying messages from last day'
                elif zoom_factor == 2:
                    self.zoom_period_label = 'Displaying messages from last week'
                elif zoom_factor == 3:
                    self.zoom_period_label = 'Displaying messages from last month'
                elif zoom_factor == 4:
                    self.zoom_period_label = 'Displaying messages from last three months'
                elif zoom_factor == 5:
                    self.zoom_period_label = 'Displaying messages from last six months'
                elif zoom_factor == 6:
                    self.zoom_period_label = 'Displaying messages from last year'
                elif zoom_factor == 7:
                    self.zoom_period_label = 'Displaying all messages'
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

    @allocate_autorelease_pool
    @run_in_gui_thread
    def render_history_messages(self, messages, scrollToMessageId=None):
        if self.chatViewController.scrolling_zoom_factor:
            if not self.message_count_from_history:
                self.message_count_from_history = len(messages)
                self.chatViewController.lastMessagesLabel.setStringValue_(self.zoom_period_label)
            else:
                if self.message_count_from_history >= len(messages):
                    self.chatViewController.setHandleScrolling_(False)
                    self.zoom_period_label = '%s. There are no previous messages.' % self.zoom_period_label
                    self.chatViewController.lastMessagesLabel.setStringValue_(self.zoom_period_label)
                    self.chatViewController.setHandleScrolling_(False)
                else:
                    self.chatViewController.lastMessagesLabel.setStringValue_(self.zoom_period_label)
        else:
            self.message_count_from_history = len(messages)
            if len(messages):
                self.chatViewController.lastMessagesLabel.setStringValue_('Scroll up for going back in time')
            else:
                self.chatViewController.setHandleScrolling_(False)
                self.chatViewController.lastMessagesLabel.setStringValue_('There are no previous messages')

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

        for message in messages:
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
                if call_id is not None and call_id != message.sip_callid and  message.media_type == 'chat':
                    self.chatViewController.showSystemMessage(message.sip_callid, 'Connection established', timestamp, False)

                if message.media_type == 'sms' and last_media_type == 'chat':
                    self.chatViewController.showSystemMessage(message.sip_callid, 'Instant messages', timestamp, False)

                self.chatViewController.showMessage(message.sip_callid, message.msgid, message.direction, message.cpim_from, icon, message.body, timestamp, is_private=private, recipient=message.cpim_to, state=message.status, is_html=is_html, history_entry=True, media_type = message.media_type, encryption=message.encryption)

            call_id = message.sip_callid
            last_media_type = 'chat' if message.media_type == 'chat' else 'sms'

        if scrollToMessageId is not None:
            self.chatViewController.scrollToId(scrollToMessageId)
        self.chatViewController.loadingProgressIndicator.stopAnimation_(None)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def resend_last_failed_message(self, messages):
        if self.sessionController.account is BonjourAccount():
            return

        for message in messages:
            if message.cpim_to:
                address, display_name, full_uri, fancy_uri = sipuri_components_from_string(message.cpim_to)
                try:
                    recipient = CPIMIdentity.parse('%s <sip:%s>' % (display_name, address))
                except ValueError:
                    continue
            else:
                recipient = None

            private = True if message.private == "1" else False
            self.outgoing_message_handler.resend(message.msgid, message.body, recipient, private)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def send_pending_message(self):
        if self.sessionController.pending_chat_messages:
            for message in reversed(self.sessionController.pending_chat_messages.values()):
                self.outgoing_message_handler.resend(message.msgid, message.text, message.recipient, message.private)
            self.sessionController.pending_chat_messages = {}

    def chatViewDidGetNewMessage_(self, chatView):
        NSApp.delegate().noteNewMessage(self.chatViewController.outputView.window())
        if self.mustShowUnreadMessages:
            self.chatWindowController.noteNewMessageForSession_(self.sessionController)

    def updateEncryptionWidgets(self):
        if self.status == STREAM_CONNECTED:
            ctx = self.otr_account.getContext(self.sessionController.call_id)
            fingerprint = ctx.getCurrentKey()
            otr_fingerprint_verified = self.otr_account.getTrust(self.sessionController.remoteSIPAddress, str(fingerprint))
            if self.is_encrypted:
                if otr_fingerprint_verified:
                    self.chatWindowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-green"))
                else:
                    if self.otr_account.getTrusts(self.sessionController.remoteSIPAddress):
                        self.chatWindowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-red"))
                        try:
                            self.new_fingerprints[str(fingerprint)]
                        except KeyError:
                            self.new_fingerprints[str(fingerprint)] = True
                            self.notify_changed_fingerprint()
                    else:
                        self.chatWindowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-orange"))
            else:
                settings = SIPSimpleSettings()
                if not settings.chat.enable_encryption:
                    self.chatWindowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("unlocked-darkgray"))
                else:
                    if self.sessionController.remote_focus:
                        self.chatWindowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("unlocked-darkgray"))
                    else:
                        self.chatWindowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-green" if self.is_encrypted else "unlocked-red"))
        else:
            self.chatWindowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("unlocked-darkgray"))


    def updateToolbarButtons(self, toolbar, got_proposal=False):
        """Called by ChatWindowController when receiving various middleware notifications"""
        settings = SIPSimpleSettings()

        if self.sessionController.hasStreamOfType("audio"):
            audio_stream = self.sessionController.streamHandlerOfType("audio")

        for item in toolbar.visibleItems():
            identifier = item.itemIdentifier()
            if identifier == 'encryption':
                self.updateEncryptionWidgets()
                item.setEnabled_(True)

            elif identifier == 'connect_button':
                if self.status in (STREAM_CONNECTING, STREAM_WAITING_DNS_LOOKUP):
                    item.setEnabled_(True)
                    item.setToolTip_('Cancel chat')
                    item.setLabel_(u'Cancel')
                    item.setImage_(NSImage.imageNamed_("stop_chat"))
                elif self.status == STREAM_PROPOSING:
                    if self.sessionController.proposalOriginator == 'remote':
                        item.setEnabled_(False)
                    else:
                        item.setToolTip_('Cancel chat')
                        item.setLabel_(u'Cancel')
                        item.setImage_(NSImage.imageNamed_("stop_chat"))
                        item.setEnabled_(True)
                elif self.status == STREAM_CONNECTED:
                    item.setEnabled_(True)
                    item.setToolTip_('End chat')
                    item.setLabel_(u'Disconnect')
                    item.setImage_(NSImage.imageNamed_("stop_chat"))
                else:
                    item.setEnabled_(not self.sessionController.inProposal)
                    item.setToolTip_('Start chat')
                    item.setLabel_(u'Connect')
                    item.setImage_(NSImage.imageNamed_("start_chat"))
            elif identifier == 'audio':
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_CONNECTED:
                        item.setToolTip_('Remove audio')
                        item.setImage_(NSImage.imageNamed_("hangup"))
                    elif audio_stream.status in (STREAM_PROPOSING, STREAM_RINGING):
                        item.setToolTip_('Cancel audio')
                        item.setImage_(NSImage.imageNamed_("hangup"))
                else:
                    if self.sessionController.state == STATE_IDLE:
                        item.setToolTip_('Start audio')
                    else:
                        item.setToolTip_('Add audio')
                    item.setImage_(NSImage.imageNamed_("audio"))
            elif identifier == 'hold':
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_CONNECTED:
                        if audio_stream.holdByRemote:
                            item.setImage_(NSImage.imageNamed_("paused"))
                        elif audio_stream.holdByLocal:
                            item.setImage_(NSImage.imageNamed_("paused"))
                        else:
                            item.setImage_(NSImage.imageNamed_("pause"))
                    else:
                        item.setImage_(NSImage.imageNamed_("pause"))
                else:
                    item.setImage_(NSImage.imageNamed_("pause"))
            elif identifier == 'record':
                item.setImage_(NSImage.imageNamed_("recording1" if self.sessionController.hasStreamOfType("audio") and audio_stream.status == STREAM_CONNECTED and audio_stream.stream.recording_active else "record"))
            elif identifier == 'video' and self.sessionControllersManager.isMediaTypeSupported('video'):
                if self.sessionController.hasStreamOfType("video"):
                    video_stream = self.sessionController.streamHandlerOfType("video")
                    if video_stream.status == STREAM_PROPOSING or video_stream.status == STREAM_RINGING:
                        item.setToolTip_('Cancel video call')
                        item.setImage_(NSImage.imageNamed_("hangup"))
                    elif video_stream.status == STREAM_CONNECTED:
                        item.setToolTip_('Hangup video call')
                        item.setImage_(NSImage.imageNamed_("hangup"))
                else:
                    item.setToolTip_('Add video to session')
                    item.setImage_(NSImage.imageNamed_("video"))
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
                item.setEnabled_(True if self.status == STREAM_CONNECTED and self.sessionControllersManager.isMediaTypeSupported('file-transfer') else False)
            elif identifier == 'sendfile':
                item.setEnabled_(True if self.status == STREAM_CONNECTED and self.sessionControllersManager.isMediaTypeSupported('file-transfer') else False)

    def notify_changed_fingerprint(self):
        log_text = '%s changed encryption fingerprint. Please verify it again.' % self.sessionController.getTitleShort()
        self.showSystemMessage(log_text, ISOTimestamp.now(), True)

        settings = SIPSimpleSettings()
        if not settings.audio.silent:
            this_hour = int(datetime.datetime.now(tzlocal()).strftime("%H"))
            volume = 0.8

            if settings.sounds.night_volume.start_hour < settings.sounds.night_volume.end_hour:
                if this_hour < settings.sounds.night_volume.end_hour and this_hour >= settings.sounds.night_volume.start_hour:
                    volume = settings.sounds.night_volume.volume/100.0
            elif settings.sounds.night_volume.start_hour > settings.sounds.night_volume.end_hour:
                if this_hour < settings.sounds.night_volume.end_hour:
                    volume = settings.sounds.night_volume.volume/100.0
                elif this_hour >=  settings.sounds.night_volume.start_hour:
                    volume = settings.sounds.night_volume.volume/100.0

            NSApp.delegate().contactsWindowController.speech_synthesizer.setVolume_(volume)
            NSApp.delegate().contactsWindowController.speech_synthesizer.startSpeakingString_(log_text)

        nc_title = 'Chat Encryption Warning'
        nc_subtitle = self.sessionController.getTitleShort()
        nc_body = 'Encryption fingerprint has changed'
        NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)

    def validateToolbarButton(self, item):
        """
        Called automatically by Cocoa in ChatWindowController to enable/disable each toolbar item
        """

        if self.sessionController.hasStreamOfType("audio"):
            audio_stream = self.sessionController.streamHandlerOfType("audio")

        if hasattr(item, 'itemIdentifier'):
            identifier = item.itemIdentifier()
            if identifier == NSToolbarPrintItemIdentifier and NSApp.delegate().applicationName != 'Blink Lite':
                return True

            if identifier == 'encryption':
                self.updateEncryptionWidgets()
                return True

            elif identifier == 'connect_button':
                if self.status in (STREAM_CONNECTING, STREAM_WAITING_DNS_LOOKUP):
                    return True
                elif self.status in (STREAM_PROPOSING, STREAM_CONNECTED):
                    return True if self.sessionController.canCancelProposal() else False
                else:
                    return True if self.sessionController.canProposeMediaStreamChanges() else False
            elif identifier == 'audio':
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_CONNECTED:
                        return True if self.sessionController.canProposeMediaStreamChanges() else False
                    elif audio_stream.status in (STREAM_PROPOSING, STREAM_RINGING):
                        return True if self.sessionController.canCancelProposal() else False
                    else:
                        return True if self.sessionController.canProposeMediaStreamChanges() else False
                else:
                    return True if self.sessionController.canProposeMediaStreamChanges() else False
            elif identifier == 'hold':
                if self.sessionController.hasStreamOfType("audio") and audio_stream.status == STREAM_CONNECTED:
                    return True
            elif identifier == 'record':
                return True if self.sessionController.hasStreamOfType("audio") and audio_stream.status == STREAM_CONNECTED and NSApp.delegate().applicationName != 'Blink Lite' else False
            elif identifier == 'maximize' and self.video_frame_visible:
                return True
            elif identifier == 'video' and self.status == STREAM_CONNECTED and self.sessionControllersManager.isMediaTypeSupported('video'):
                if self.sessionController.hasStreamOfType("video"):
                    video_stream = self.sessionController.streamHandlerOfType("video")
                    if video_stream.status == STREAM_CONNECTED:
                        return True if self.sessionController.canProposeMediaStreamChanges() else False
                    elif video_stream.status in (STREAM_PROPOSING, STREAM_RINGING):
                        return True if self.sessionController.canCancelProposal() else False
                    else:
                        return True if self.sessionController.canProposeMediaStreamChanges() else False
                else:
                    return True if self.sessionController.canProposeMediaStreamChanges() else False
            elif identifier == 'sendfile' and self.sessionControllersManager.isMediaTypeSupported('file-transfer') and self.status == STREAM_CONNECTED:
                return True
            elif identifier == 'smileys':
                return True
            elif identifier == 'editor' and self.sessionController.account is not BonjourAccount():
                settings = SIPSimpleSettings()
                if not settings.chat.disable_collaboration_editor:
                    return True
            elif identifier == 'history' and NSApp.delegate().applicationName != 'Blink Lite':
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
                return self.status == STREAM_CONNECTED

        return False

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
                    self.endStream()
                else:
                    if self.sessionController.canProposeMediaStreamChanges():
                        if len(self.sessionController.streamHandlers) > 1:
                            self.sessionController.addChatToSession()
                        elif self.status in (STREAM_IDLE, STREAM_FAILED):
                            self.sessionController.startChatSession()
                    else:
                        self.sessionController.log_info(u"Session has a pending proposal")

            elif identifier == 'audio':
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_PROPOSING:
                        self.sessionController.cancelProposal(audio_stream)
                    else:
                        if self.status == STREAM_CONNECTED:
                            self.sessionController.removeAudioFromSession()
                            sender.setToolTip_('Add audio to session')
                        else:
                            self.sessionController.endStream(audio_stream)
                            sender.setToolTip_('Start audio session')

                    sender.setImage_(NSImage.imageNamed_("audio"))

                    # The button will be enabled again after operation is finished
                    sender.setEnabled_(False)
                else:
                    if self.sessionController.state == STATE_IDLE:
                        self.notification_center.add_observer(self, sender=self.sessionController)
                        self.sessionController.startCompositeSessionWithStreamsOfTypes(("audio", "chat"))
                    else:
                        self.sessionController.addAudioToSession()
                        self.notification_center.post_notification("SIPSessionGotRingIndication", sender=self.sessionController.session)

                    sender.setToolTip_('Cancel audio call')
                    sender.setImage_(NSImage.imageNamed_("hangup"))

            elif identifier == 'record' and NSApp.delegate().applicationName != 'Blink Lite':
                if audio_stream.stream.recording_active:
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

            elif identifier == 'video':
                self.toggleVideoFrame()
                sender.setImage_(NSImage.imageNamed_("video") if not self.video_frame_visible else NSImage.imageNamed_("video-hangup"))
                # TODO: add interaction with video stream from middleware -adi
                return
                if self.status == STREAM_CONNECTED:
                    if self.sessionController.hasStreamOfType("video"):
                        if video_stream.status == STREAM_PROPOSING or video_stream.status == STREAM_RINGING:
                            self.sessionController.cancelProposal(video_stream)
                        else:
                            self.sessionController.removeVideoFromSession()

                        sender.setToolTip_('Add video to session')

                        # The button will be enabled again after operation is finished
                        sender.setEnabled_(False)
                    else:
                        self.sessionController.addVideoToSession()
                        sender.setToolTip_('Cancel the video call')

            elif identifier == 'maximize':
                self.enterFullScreen()

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
                sender.setToolTip_("Switch back to chat session" if self.chatViewController.editorVisible else "Show collaborative editor")
            elif identifier == 'history' and NSApp.delegate().applicationName != 'Blink Lite':
                contactWindow = NSApp.delegate().contactsWindowController
                contactWindow.showHistoryViewer_(None)
                if self.sessionController.account is BonjourAccount():
                    contactWindow.historyViewer.filterByURIs(('bonjour', ))
                else:
                    contactWindow.historyViewer.filterByURIs((format_identity_to_string(self.sessionController.target_uri),))
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

    def userClickedScreenshotMenu_(self, sender):
        screenshots_folder = ApplicationData.get('.tmp_screenshots')
        if not os.path.exists(screenshots_folder):
            os.mkdir(screenshots_folder, 0700)
        filename = '%s/xscreencapture.png' % screenshots_folder
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

    def toggleScreensharingWithConferenceParticipants(self):
        self.share_screen_in_conference = True if not self.share_screen_in_conference else False
        if self.share_screen_in_conference and self.stream is not None:
            self.sessionController.log_info(u"Start sharing screen with conference participants")
            self.screensharing_handler.setConnected(self.stream)
        else:
            self.screensharing_handler.setDisconnected()
            self.sessionController.log_info(u"Stop sharing screen with conference participants")

        self.setScreenSharingToolbarIcon()

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
        menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL).setTitle_("Share My Screen with Conference Participants" if self.share_screen_in_conference == False else "Stop Screen Sharing")
        self.chatWindowController.noteSession_isScreenSharing_(self.sessionController, self.share_screen_in_conference)

        self.chatWindowController.setScreenSharingToolbarIconSize()

    def resetEditorToolbarIcon(self):
        try:
            item = (item for item in self.chatWindowController.toolbar.visibleItems() if item.tag() == 109).next()
        except StopIteration:
            pass
        else:
            item.setImage_(NSImage.imageNamed_("editor"))

    def checkScreenshotTaskStatus_(self, notification):
        status = notification.object().terminationStatus()
        if status == 0 and self.sessionController and os.path.exists(self.screencapture_file):
            self.sendFiles([unicode(self.screencapture_file)])
        NSNotificationCenter.defaultCenter().removeObserver_name_object_(self, NSTaskDidTerminateNotification, self.screenshot_task)
        self.screenshot_task = None

    def toggleEditor(self):
        self.chatViewController.editorIsComposing = False
        self.showChatViewWithEditorWhileVideoActive()
        self.chatViewController.toggleCollaborationEditor()
        self.chatWindowController.noteSession_isComposing_(self.sessionController, False)

    def remoteBecameIdle_(self, timer):
        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
        self.remoteTypingTimer = None
        self.chatWindowController.noteSession_isComposing_(self.sessionController, False)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_ChatStreamGotMessage(self, stream, data):
        message = data.message
        if message.content_type == 'application/blink-logging-status':
            if message.body.lower() == "disabled":
                self.remote_party_history = False
                if not self.disable_chat_history:
                    log = NSLocalizedString("Remote logging disabled", "Chat system message")
                    nc_title = NSLocalizedString("Chat History Logging", "System notification title")
                    nc_subtitle = self.sessionController.getTitleShort()
                    NSApp.delegate().gui_notify(nc_title, log, nc_subtitle)
            else:
                self.remote_party_history = True
                if not self.disable_chat_history:
                    log = NSLocalizedString("Remote logging enabled", "Chat system message")
                    nc_title = NSLocalizedString("Chat History Logging", "System notification title")
                    nc_subtitle = self.sessionController.getTitleShort()
                    NSApp.delegate().gui_notify(nc_title, log, nc_subtitle)

            self.sessionController.log_info(log)
            self.updateDatabaseRecordingButton()

        elif message.content_type == 'application/blink-icon':
            if not self.session.remote_focus:
                try:
                    self.remoteIcon = decode_icon(message.body)
                except Exception:
                    pass
                else:
                    self.chatWindowController.refreshDrawer()
            else:
                pass
                # TODO: update icons for the contacts in the drawer
            return

        if not message.content_type.startswith("text/"):
            return

        hash = hashlib.sha1()
        hash.update(message.body.encode("utf-8")+str(message.timestamp))
        msgid = hash.hexdigest()

        if msgid not in self.history_msgid_list:
            sender = message.sender
            recipient = message.recipients[0]
            private = data.private
            text = message.body
            sender_aor = format_identity_to_string(sender)
            status = 'delivered'
            encryption = ''

            if not self.sessionController.remote_focus:
                try:
                    ctx = self.otr_account.getContext(self.sessionController.call_id)
                    text, tlvs = ctx.receiveMessage(text.encode('utf-8'), appdata={'stream': self.stream})
                    self.setEncryptionState(ctx)
                    fingerprint = ctx.getCurrentKey()
                    if fingerprint:
                        otr_fingerprint_verified = self.otr_account.getTrust(self.sessionController.remoteSIPAddress, str(fingerprint))
                        if otr_fingerprint_verified:
                            encryption = 'verified'
                        else:
                            encryption = 'unverified'

                    self.chatOtrSmpWindow.handle_tlv(tlvs)

                    if text is None:
                        return
                except potr.context.NotOTRMessage, e:
                    self.sessionController.log_debug('Message %s is not an OTR message' % msgid)
                except potr.context.UnencryptedMessage, e:
                    encryption = 'failed'
                    status = 'failed'
                    log = 'Message %s is not encrypted, while encryption was expected' % msgid
                    self.sessionController.log_error(log)
                    self.showSystemMessage(log, ISOTimestamp.now(), True)
                except potr.context.NotEncryptedError, e:
                    encryption = 'failed'
                    # we got some encrypted data
                    log = 'Encrypted message %s is unreadable, as encryption is disabled' % msgid
                    status = 'failed'
                    self.sessionController.log_error(log)
                    self.showSystemMessage(log, ISOTimestamp.now(), True)
                    return
                except potr.context.ErrorReceived, e:
                    status = 'failed'
                    # got a protocol error
                    log = 'Encrypted message %s protocol error: %s' % (msgid, e.args[0].error)
                    self.sessionController.log_error(log)
                    self.showSystemMessage(log, ISOTimestamp.now(), True)
                    return
                except potr.crypt.InvalidParameterError, e:
                    encryption = 'failed'
                    status = 'failed'
                    # received a packet we cannot process (probably tampered or
                    # sent to wrong session)
                    log = 'Invalid encrypted message received' % msgid
                    self.sessionController.log_error(log)
                    self.showSystemMessage(log, ISOTimestamp.now(), True)
                except RuntimeError, e:
                    encryption = 'failed'
                    status = 'failed'
                    self.sessionController.log_error('Encrypted message has runtime error: %s' % e)

            # It was encoded earlier because potr only supports bytes
            text = text.decode('utf-8')
            if text.startswith('?OTR:'):
                return

            timestamp = message.timestamp
            is_html = True if message.content_type == 'text/html' else False
            name = format_identity_to_string(sender, format='compact')
            icon = NSApp.delegate().contactsWindowController.iconPathForURI(sender_aor, self.session.remote_focus)
            recipient_html = '%s <%s@%s>' % (recipient.display_name, recipient.uri.user, recipient.uri.host) if recipient else ''
            if self.chatViewController:
                self.chatViewController.showMessage(self.sessionController.call_id, msgid, 'incoming', name, icon, text, timestamp, is_private=private, recipient=recipient_html, state=status, is_html=is_html, media_type='chat', encryption=encryption)

            tab = self.chatViewController.outputView.window()
            tab_is_key = tab.isKeyWindow() if tab else False
            tab = None

            # FancyTabViewSwitcher will set unfocused tab item views as Hidden
            if not tab_is_key or self.chatViewController.view.isHiddenOrHasHiddenAncestor():
                # notify growl
                growl_data = NotificationData()
                growl_data.sender = format_identity_to_string(sender, format='compact')
                growl_data.content = html2txt(text[0:400]) if message.content_type == 'text/html' else text[0:400]
                NotificationCenter().post_notification("GrowlGotChatMessage", sender=self, data=growl_data)
                NSApp.requestUserAttention_(NSInformationalRequest)

                nc_title = 'Chat Message Received'
                nc_subtitle = format_identity_to_string(sender, format='full')
                nc_body = html2txt(text[0:400]) if message.content_type == 'text/html' else text[0:400]
                NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)

            NotificationCenter().post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=NotificationData(direction='incoming', history_entry=False, remote_party=format_identity_to_string(self.sessionController.remotePartyObject, format='full'), local_party=format_identity_to_string(self.sessionController.account) if self.sessionController.account is not BonjourAccount() else 'bonjour', check_contact=True))

            # save to history
            if 'Welcome to SylkServer!' not in text:
                message = MessageInfo(msgid, direction='incoming', sender=sender, recipient=recipient, timestamp=timestamp, text=text, private=private, status="delivered", content_type='html' if is_html else 'text', encryption=encryption)
                self.outgoing_message_handler.add_to_history(message)

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

    def _NH_ChatStreamDidSetNickname(self, stream, data):
        nickname = self.nickname_request_map.pop(data.message_id)
        self.sessionController.nickname = nickname

    def _NH_ChatStreamDidNotSetNickname(self, stream, data):
        self.nickname_request_map.pop(data.message_id)

    def _NH_BlinkMuteChangedState(self, sender, data):
        self.updateToolbarMuteIcon()

    def _NH_BlinkFileTransferDidEnd(self, sender, data):
        if self.sessionController.session is None:
            return

        settings = SIPSimpleSettings()
        if not settings.file_transfer.render_incoming_image_in_chat_window and not settings.file_transfer.render_incoming_video_in_chat_window:
            return

        if self.sessionController.remoteSIPAddress != sender.remote_identity:
            NSApp.delegate().contactsWindowController.fileTransfersWindow.showWindow_(None)
            return

        if image_file_extension_pattern.search(data.file_path):
            text  = "Incoming image file transfer has finished"
            try:
                image = NSImage.alloc().initWithContentsOfFile_(data.file_path)
                w = image.size().width
                width = w if w and w < 600 else '100%'
            except Exception:
                width = '100%'

            text += "<p><img src='%s' border='0' width='%s'>" % (data.file_path, width)
        else:
            return

        if self.status == STREAM_CONNECTED:
            name = format_identity_to_string(self.sessionController.session.remote_identity, format='full')
            icon = NSApp.delegate().contactsWindowController.iconPathForURI(format_identity_to_string(self.sessionController.session.remote_identity))
            timestamp = ISOTimestamp.now()
            if self.chatViewController:
                self.chatViewController.showMessage(self.sessionController.call_id, str(uuid.uuid1()), 'incoming', name, icon, text, timestamp, state="delivered", history_entry=True, is_html=True, media_type='chat')

    def _NH_BlinkSessionDidEnd(self, sender, data):
        self.reset()

    def _NH_BlinkSessionDidFail(self, sender, data):
        reason = data.failure_reason or data.reason
        if reason != 'Session Cancelled':
            if self.last_failure_reason != reason:
                self.last_failure_reason = reason
        if not self.mediastream_failed and not self.mediastream_ended:
            if reason != 'Session Cancelled':
                message = "Connection failed: %s" % reason.title()
                self.showSystemMessage(message, ISOTimestamp.now(), True)

        self.changeStatus(STREAM_IDLE, self.sessionController.endingBy)
        self.reset()

    def _NH_BlinkSessionDidStart(self, sender, data):
        self.session_succeeded = True
        # toggle collaborative editor to initialize the java script to be able to receive is-composing
        self.last_failure_reason = None
        settings = SIPSimpleSettings()
        if self.sessionController.account is not BonjourAccount() and not settings.chat.disable_collaboration_editor:
            self.toggleEditor()
            self.toggleEditor()

    def _NH_BlinkProposalDidFail(self, sender, data):
        if self.last_failure_reason != data.failure_reason:
            message = "Proposal failed: %s" % data.failure_reason
            self.last_failure_reason = data.failure_reason
            self.showSystemMessage(message, ISOTimestamp.now(), True)

    def _NH_BlinkProposalGotRejected(self, sender, data):
        if data.code != 487:
            if self.last_failure_reason != data.reason:
                self.last_failure_reason = data.reason
                reason = 'Remote party failed to establish the connection' if data.reason == 'Internal Server Error' else '%s (%s)' % (data.reason,data.code)
                message = "Proposal rejected: %s" % reason if data.code != 200 else "Proposal rejected"
                self.showSystemMessage(message, ISOTimestamp.now(), True)

    def _NH_MediaStreamDidStart(self, sender, data):
        if self.stream is None or self.stream.msrp is None: # stream may have ended in the mean time
            return
        self.changeStatus(STREAM_CONNECTED)

        self.init_otr(disable_encryption=self.sessionController.remote_focus)

        self.mediastream_started = True
        self.last_failure_reason = None
        endpoint = str(self.stream.msrp.full_remote_path[0])
        self.sessionController.log_info(u"Chat session established to %s" % endpoint)
        self.showSystemMessage("Connection established", ISOTimestamp.now())

        # Set nickname if available
        nickname = self.sessionController.nickname
        self.sessionController.nickname = None
        self.setNickname(nickname)

        self.outgoing_message_handler.setConnected(self.stream)

        # needed to set the Audio button state after session has started
        self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self)

        if self.sessionController.account is BonjourAccount():
            self.sendOwnIcon()

        self.sendLoggingState()


    def _NH_MediaStreamDidEnd(self, sender, data):
        self.changeStatus(STREAM_IDLE, self.sessionController.endingBy)
        self.mediastream_ended = True
        self.sessionController.log_info(u"Chat session ended")
        if self.mediastream_started and not self.mediastream_failed:
            self.showSystemMessage('Connection closed', ISOTimestamp.now())
            self.outgoing_message_handler.setDisconnected()

    def _NH_MediaStreamDidFail(self, sender, data):
        self.mediastream_failed = True
        self.sessionController.log_info(u"Chat session failed: %s" % data.reason)
        if data.reason in ('Connection was closed cleanly.', 'Cannot send chunk because MSRPSession is DONE'):
            reason = 'Connection lost'
        elif data.failure is not None and data.failure.type is GNUTLSError:
            reason = 'Connection error (TLS)'
        elif data.reason in ('MSRPTimeout', 'MSRPConnectTimeout', 'MSRPBindSessionTimeout', 'MSRPIncomingConnectTimeout', 'MSRPRelayConnectTimeout'):
            reason = 'Connection failed'
        elif data.reason == 'MSRPRelayAuthError':
            reason = 'Authentication failed'
        else:
            reason = data.reason

        self.showSystemMessage(reason, ISOTimestamp.now(), True)
        self.changeStatus(STREAM_FAILED, data.reason)
        self.outgoing_message_handler.setDisconnected()

    def _NH_OTRPrivateKeyDidChange(self, sender, data):
        if self.sessionController.remote_focus:
            return
        if self.status == STREAM_CONNECTED and self.is_encrypted:
            otr_context_id = self.sessionController.call_id
            ctx = self.otr_account.getContext(otr_context_id)
            ctx.disconnect()
            self.init_otr()
            self.outgoing_message_handler.propose_otr()
        else:
            self.init_otr()

        self.chatWindowController.revalidateToolbar()

    def _NH_CFGSettingsObjectDidChange(self, sender, data):
        settings = SIPSimpleSettings()
        if data.modified.has_key("chat.disable_history"):
            if self.sessionController.contact is not None and self.sessionController.contact.contact.disable_chat_history is not None:
                self.disable_chat_history = self.sessionController.contact.contact.disable_chat_history
            else:
                self.disable_chat_history = settings.chat.disable_history
            self.updateDatabaseRecordingButton()
        elif data.modified.has_key("chat.enable_encryption"):
            if self.status == STREAM_CONNECTED:
                if self.is_encrypted and not settings.chat.enable_encryption:
                    otr_context_id = self.sessionController.call_id
                    ctx = self.otr_account.getContext(otr_context_id)
                    ctx.disconnect()
                elif settings.chat.enable_encryption and not self.is_encrypted:
                    self.outgoing_message_handler.propose_otr()

            self.chatWindowController.revalidateToolbar()

    def _NH_ChatReplicationJournalEntryReceived(self, sender, data):
        if self.status == STREAM_CONNECTED:
            return

        data = data.chat_message
        if self.local_uri != data['local_uri'] or self.remote_uri != data['remote_uri']:
            return

        icon = NSApp.delegate().contactsWindowController.iconPathForURI(data['cpim_to'])
        timestamp = ISOTimestamp(data['cpim_timestamp'])
        self.chatViewController.showMessage(data['call_id'], data['msgid'], data['direction'], data['cpim_from'], icon, data['body'], timestamp, is_private=bool(int(data['private'])), recipient=data['cpim_to'], state=data['status'], is_html=True, history_entry=True, media_type='chat', encryption=data['encryption'])

    def resetIsComposingTimer(self, refresh):
        if self.remoteTypingTimer:
            # if we don't get any indications in the request refresh, then we assume remote to be idle
            self.remoteTypingTimer.setFireDate_(NSDate.dateWithTimeIntervalSinceNow_(refresh))
        else:
            self.remoteTypingTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(refresh, self, "remoteBecameIdle:", None, False)

    def endStream(self, closeTab=False):
        if self.status != STREAM_DISCONNECTING:
            self.sessionControllersManager.ringer.stop_ringing(self.sessionController.session)

        if self.status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self.stream)
            self.changeStatus(STREAM_CANCELLING)
        else:
            if closeTab:
                self.sessionController.endStream(self)
            else:
                # it we have more than chat, we could just stop the chat stream only but is counter intuitive in the GUI so we end the whole session
                self.sessionController.end()
            self.changeStatus(STREAM_DISCONNECTING)

    # lifetime of a chat controler: possible deallocation paths
    # 1. User click on close tab: closeTab -> endStream -> CloseWindow -> deallocTimer -> dealloc
    # 2. User clicks on close window: closeWindow -> for each tab -> closeTab -> endStream -> CloseWindow -> deallocTimer -> dealloc
    # 3. Session ends by remote: mediaDidEnd -> endStream -> reset -> CloseWindow -> deallocTimer -> dealloc
    # 4. User clicks on disconnect button: endStream -> reset

    def closeTab(self):
        self.endStream(True)
        if self.outgoing_message_handler:
            self.outgoing_message_handler.setDisconnected()
        if self.screensharing_handler:
            self.screensharing_handler.setDisconnected()
        self.closeWindow()
        self.notification_center.discard_observer(self, sender=self.sessionController)
        self.startDeallocTimer()

    def reset(self):
        self.outgoing_message_handler.setDisconnected()
        self.screensharing_handler.setDisconnected()

        self.notification_center.discard_observer(self, sender=self.stream)
        self.stream = ChatStream()
        self.notification_center.add_observer(self, sender=self.stream)

        self.mediastream_failed = False
        self.mediastream_ended = False
        self.session_succeeded = False
        self.mediastream_started = False
        self.last_failure_reason = None
        self.remoteIcon = None
        self.share_screen_in_conference = False
        self.previous_is_encrypted = False

        self.videoContainer.hideVideo()
        self.exitFullScreen()
        self.setScreenSharingToolbarIcon()
        self.resetEditorToolbarIcon()

        # save chat view so we can print it when session is over
        self.sessionController.chatPrintView = self.chatViewController.outputView

        self.chatWindowController.noteSession_isComposing_(self.sessionController, False)
        self.chatWindowController.noteSession_isScreenSharing_(self.sessionController, False)

    def startDeallocTimer(self):
        self.removeFromSession()
        self.otr_account = None

        if not self.session_was_active:
            self.notification_center.post_notification("BlinkChatWindowWasClosed", sender=self.sessionController)

        if not self.dealloc_timer:
            self.dealloc_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(3.0, self, "deallocTimer:", None, False)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSEventTrackingRunLoopMode)

    def deallocTimer_(self, timer):
        self.release()

    def dealloc(self):
        # remove middleware observers
        self.notification_center.remove_observer(self, name='BlinkFileTransferDidEnd')
        self.notification_center.remove_observer(self, name='BlinkMuteChangedState')
        self.notification_center.remove_observer(self, name='ChatReplicationJournalEntryReceived')
        self.notification_center.remove_observer(self, name='CFGSettingsObjectDidChange')
        self.notification_center.remove_observer(self, name='OTRPrivateKeyDidChange')

        self.notification_center = None

        # remove GUI observers
        NSNotificationCenter.defaultCenter().removeObserver_(self)

        # dealloc timers
        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
            self.remoteTypingTimer = None

        self.dealloc_timer.invalidate()
        self.dealloc_timer = None

        # release OTR check window
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
        self.sessionController = None
        self.screensharing_handler = None
        self.history = None
        self.backend = None

        BlinkLogger().log_debug(u"Dealloc %s" % self)
        super(ChatController, self).dealloc()


class MessageInfo(object):
    def __init__(self, msgid, direction='outgoing', sender=None, recipient=None, timestamp=None, text=None, private=False, status=None, content_type='text', pending=False, encryption=''):
        self.msgid = msgid
        self.direction = direction
        self.sender = sender
        self.recipient = recipient
        self.timestamp = timestamp
        self.text = text
        self.private = private
        self.status = status
        self.content_type = content_type
        self.pending = pending
        self.encryption = encryption


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

    implements(IObserver)

    stream = None
    connected = False
    delegate = None
    messages = None
    no_report_received_messages = None
    remote_uri = None
    local_uri = None
    must_propose_otr = False

    def initWithView_(self, chatView):
        self = super(OutgoingMessageHandler, self).init()
        if self:
            self.stream = None
            self.connected = None
            self.messages = {}
            self.no_report_received_messages = {}
            self.history = ChatHistory()
            self.delegate = chatView
            self.local_uri = '%s@%s' % (self.delegate.account.id.username, self.delegate.account.id.domain) if self.delegate.account is not BonjourAccount() else 'bonjour'
            self.remote_uri = format_identity_to_string(self.delegate.delegate.sessionController.remotePartyObject)
        return self

    def dealloc(self):
        self.delegate = None
        super(OutgoingMessageHandler, self).dealloc()

    def close(self):
        self.stream = None
        self.connected = None
        self.history = None

    def propose_otr(self):
        if self.delegate.delegate.status != STREAM_CONNECTED:
            return

        if self.delegate.delegate.sessionController.remote_focus:
            return

        self.must_propose_otr = False

        otr_context_id = self.delegate.sessionController.call_id
        ctx = self.delegate.delegate.otr_account.getContext(otr_context_id)
        newmsg = ctx.sendMessage(potr.context.FRAGMENT_SEND_ALL_BUT_LAST, '?OTRv2?', appdata={'stream':self.delegate.delegate.stream})
        self.delegate.delegate.setEncryptionState(ctx)
        try:
            self.delegate.sessionController.log_info(u"Proposing OTR...")
            self.stream.send_message(newmsg, timestamp=ISOTimestamp.now())
        except ChatStreamError:
            pass

    def _send(self, msgid):
        message = self.messages.pop(msgid)
        message.status = "sent"
        if message.private and message.recipient is not None:
            try:
                id = self.stream.send_message(message.text, timestamp=message.timestamp, recipients=[message.recipient])
                self.no_report_received_messages[msgid] = message
            except ChatStreamError, e:
                self.delegate.sessionController.log_error(u"Error sending private chat message %s: %s" % (msgid, e))
                self.delegate.markMessage(msgid, MSG_STATE_FAILED, message.private)
                message.status='failed'
                self.add_to_history(message)
                return False
        else:
            try:
                if self.delegate.delegate.sessionController.account is BonjourAccount():
                    newmsg = message.text
                elif self.delegate.delegate.sessionController.remote_focus:
                    newmsg = message.text
                else:
                    otr_context_id = self.delegate.sessionController.call_id
                    ctx = self.delegate.delegate.otr_account.getContext(otr_context_id)
                    newmsg = ctx.sendMessage(potr.context.FRAGMENT_SEND_ALL_BUT_LAST, message.text.encode('utf-8'), appdata={'stream':self.delegate.delegate.stream})
                    newmsg = newmsg.decode('utf-8')
                    self.delegate.delegate.setEncryptionState(ctx)
                    fingerprint = ctx.getCurrentKey()
                    otr_fingerprint_verified = self.delegate.delegate.otr_account.getTrust(self.delegate.sessionController.remoteSIPAddress, str(fingerprint))
                    if otr_fingerprint_verified:
                        message.encryption = 'verified'
                    else:
                        message.encryption = 'unverified'

                id = self.stream.send_message(newmsg, timestamp=message.timestamp)
                self.no_report_received_messages[msgid] = message
                if 'has requested end-to-end encryption but this software does not support this feature' in newmsg:
                    self.delegate.sessionController.log_error(u"Error sending chat message %s: OTR not started remotely" % msgid)
                    message.status='failed'
                    self.delegate.showSystemMessage(self.delegate.sessionController.call_id, "Remote party has not started OTR protocol", ISOTimestamp.now(), True)
            except potr.context.NotEncryptedError, e:
                self.delegate.sessionController.log_error('Chat message was not send. Either end your private OTR conversation, or restart it')
                return False
            except ChatStreamError, e:
                self.delegate.sessionController.log_error(u"Error sending chat message %s: %s" % (msgid, e))
                self.delegate.markMessage(msgid, MSG_STATE_FAILED, message.private)
                message.status='failed'
                self.add_to_history(message)
                return False

        self.messages[id] = message

        return id

    def send(self, text, recipient=None, private=False):
        timestamp = ISOTimestamp.now()
        icon = NSApp.delegate().contactsWindowController.iconPathForSelf()
        recipient_html = "%s <%s@%s>" % (recipient.display_name, recipient.uri.user, recipient.uri.host) if recipient else ''

        leftover = text
        while leftover:
            # if the text is too big, break it in a smaller size without corrupting
            # utf-8 character sequences
            if len(leftover) > MAX_MESSAGE_LENGTH:
                text = leftover[:MAX_MESSAGE_LENGTH]
                while len(text.encode("utf-8")) > MAX_MESSAGE_LENGTH:
                    text = text[:-1]
                leftover = leftover[len(text):]
            else:
                text = leftover
                leftover = ""

            hash = hashlib.sha1()
            hash.update(text.encode("utf-8")+str(timestamp))
            msgid = hash.hexdigest()

            self.messages[msgid] = MessageInfo(msgid, sender=self.delegate.account, recipient=recipient, timestamp=timestamp, text=text, private=private, status="queued")

            if self.connected:
                try:
                    id = self._send(msgid)
                except Exception, e:
                    self.delegate.sessionController.log_error(u"Error sending chat message %s: %s" % (msgid, e))
                    self.delegate.showMessage(self.delegate.sessionController.call_id, msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="failed", recipient=recipient_html)
                else:
                    self.delegate.showMessage(self.delegate.sessionController.call_id, msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="sent", recipient=recipient_html, encryption=self.messages[id].encryption)
            else:
                self.messages[msgid].pending=True
                self.delegate.showMessage(self.delegate.sessionController.call_id, msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="queued", recipient=recipient_html)

        return True

    def resend(self, msgid, text, recipient=None, private=False):
        timestamp = ISOTimestamp.now()
        recipient_html = "%s <%s@%s>" % (recipient.display_name, recipient.uri.user, recipient.uri.host) if recipient else ''
        icon = NSApp.delegate().contactsWindowController.iconPathForSelf()

        self.messages[msgid] = MessageInfo(msgid=msgid, recipient=recipient, timestamp=timestamp, text=text, private=private, status="queued")

        if self.connected:
            try:
                id = self._send(msgid)
            except Exception, e:
                self.delegate.sessionController.log_error(u"Error sending chat message %s: %s" % (msgid, e))
                self.delegate.showSystemMessage(self.delegate.sessionController.call_id, "Message delivery failure", timestamp, True)
            else:
                self.delegate.showMessage(self.delegate.sessionController.call_id, msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="sent", recipient=recipient_html, encryption=self.messages[id].encryption)
        else:
            self.messages[msgid].pending=True
            self.delegate.showMessage(self.delegate.sessionController.call_id, msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="queued", recipient=recipient_html)

    def setConnected(self, stream):
        self.stream = stream
        self.no_report_received_messages = {}
        self.connected = True
        if self.delegate.delegate.require_encryption and not self.delegate.delegate.sessionController.remote_focus:
            if self.delegate.delegate.sessionController.session.direction == 'outgoing':
                self.propose_otr()
            else:
                # To avoid state race conditions where both clients propose OTR at the same time we postpone proposal for later when we type something
                self.must_propose_otr = True

        NotificationCenter().add_observer(self, sender=stream)
        pending = (msgid for msgid in self.messages.keys() if self.messages[msgid].pending)
        for msgid in pending:
            try:
                private = self.messages[msgid].private
            except KeyError:
                continue
            else:
                sent = self._send(msgid)
                if not sent:
                    self.delegate.sessionController.log_error(u"Error sending queued message: %s" % msgid)
                else:
                    self.delegate.markMessage(msgid, MSG_STATE_SENDING, private)

    def setDisconnected(self):
        self.connected = False
        pending = (msgid for msgid in self.messages.keys() if self.messages[msgid].pending)
        for msgid in pending:
            try:
                message = self.messages.pop(msgid)
            except KeyError:
                pass
            else:
                message.status = 'failed'
                self.delegate.sessionController.log_error(u"Error sending chat message %s" % msgid)
                self.delegate.markMessage(msgid, MSG_STATE_FAILED)
                self.add_to_history(message)

        self.messages = {}
        for msgid in self.no_report_received_messages.keys():
            try:
                message = self.no_report_received_messages.pop(msgid)
            except KeyError:
                pass
            else:
                self.delegate.sessionController.log_error(u"No delivery report received for chat message %s" % msgid)
                self.delegate.markMessage(msgid, MSG_STATE_FAILED)
                self.add_to_history(message)

        if self.stream:
            NotificationCenter().discard_observer(self, sender=self.stream)
            self.stream = None

    def markMessage(self, message, state):
        message.state = state
        self.delegate.markMessage(message.msgid, state, message.private)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

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

    def _NH_ChatStreamDidNotDeliverMessage(self, sender, data):
        try:
            message = self.messages.pop(data.message_id)
            if message:
                try:
                    del self.no_report_received_messages[message.msgid]
                except KeyError:
                    pass

                message.status = 'failed'
                self.delegate.sessionController.log_error(u"Chat message %s to %s was not delivered" % message.msgid)
                self.markMessage(message, MSG_STATE_FAILED)
                self.add_to_history(message)
        except KeyError:
            pass

    @allocate_autorelease_pool
    @run_in_green_thread
    def add_to_history(self, message):
        if self.delegate.delegate.disable_chat_history:
            return

        if not self.delegate.delegate.remote_party_history:
            return

        # writes the record to the sql database
        cpim_to = "%s <%s@%s>" % (message.recipient.display_name, message.recipient.uri.user, message.recipient.uri.host) if message.recipient else ''
        cpim_from = format_identity_to_string(message.sender, format='full') if message.sender else ''
        cpim_timestamp = str(message.timestamp)
        private = "1" if message.private else "0"
        self.history.add_message(message.msgid, 'chat', self.local_uri, self.remote_uri, message.direction, cpim_from, cpim_to, cpim_timestamp, message.text, message.content_type, private, message.status, call_id=self.delegate.sessionController.call_id, encryption=message.encryption)


class ConferenceScreenSharingHandler(object):
    implements(IObserver)

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
    compression = 0.5 # jpeg compression
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
        self.delegate = None
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

    @allocate_autorelease_pool
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

            jpeg = NSBitmapImageRep.alloc().initWithData_(image.TIFFRepresentation()).representationUsingType_properties_(NSJPEGFileType, {NSImageCompressionFactor: self.compression})
            # this also works and produces the same result, but it's not documented anywhere
            #jpeg = image.IKIPJPEGDataWithMaxSize_compression_(image.size().width, self.compression)

            if self.log_first_frame:
                self.delegate.sessionController.log_info('Sending %s bytes %s screen width' % (format_size(len(jpeg)), image.size().width))
                self.log_first_frame = False
            self.delegate.sessionController.log_debug('Sending %s bytes %s screen width ' % (format_size(len(jpeg)), image.size().width))
            self.may_send = False
            if self.stream:
                self.stream.send_message(str(jpeg), content_type='application/blink-screensharing', timestamp=ISOTimestamp.now())

