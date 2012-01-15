# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *
from Quartz import *

import base64
import datetime
import hashlib
import os
import time
import unicodedata
import uuid

from application.notification import IObserver, NotificationCenter
from application.system import makedirs
from application.python import Null
from dateutil.tz import tzlocal
from itertools import chain
from zope.interface import implements

from resources import ApplicationData
from sipsimple.account import BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams import ChatStream, ChatStreamError
from sipsimple.streams.applications.chat import CPIMIdentity
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import TimestampedNotificationData, Timestamp

from util import *

import SessionController
import ChatWindowManager

from BlinkLogger import BlinkLogger
from ChatViewController import *

from VideoView import VideoView
from FileTransferWindowController import openFileTransferSelectionDialog
from HistoryManager import ChatHistory
from MediaStream import *
from SIPManager import SIPManager
from SmileyManager import SmileyManager
from ScreensharingPreviewPanel import ScreensharingPreviewPanel

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

TOOLBAR_SCREENSHARING_BUTTON = 200
TOOLBAR_SCREENSHARING_MENU_REQUEST_REMOTE = 201
TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL = 202
TOOLBAR_SCREENSHARING_MENU_CANCEL = 203

TOOLBAR_SCREENSHOT_MENU_WINDOW = 301
TOOLBAR_SCREENSHOT_MENU_AREA = 302

TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU = 400
TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_HIGH = 401
TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_LOW = 402
TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_MEDIUM = 403
TOOLBAR_SCREENSHOT_WINDOW_MENU = 500

bundle = NSBundle.bundleWithPath_('/System/Library/Frameworks/Carbon.framework')
objc.loadBundleFunctions(bundle, globals(), (('SetSystemUIMode', 'III', " Sets the presentation mode for system-provided user interface elements."),))


def userClickedToolbarButtonWhileDisconnected(sessionController, sender):
    """
    Called by ChatWindowController when dispatching toolbar button clicks to the selected Session tab.
    """
    if hasattr(sender, 'itemIdentifier'):
        identifier = sender.itemIdentifier()
        if identifier == 'connect_button':
            BlinkLogger().log_info(u"Re-establishing session to %s" % sessionController.remoteParty)
            if sessionController.canProposeMediaStreamChanges():
                BlinkLogger().log_info(u"Re-establishing session to %s" % sessionController.remoteParty)
                sessionController.startChatSession()
        elif identifier == 'history' and NSApp.delegate().applicationName != 'Blink Lite':
            contactWindow = sessionController.owner
            contactWindow.showHistoryViewer_(None)
            if sessionController.account is BonjourAccount():
                contactWindow.historyViewer.filterByContact('bonjour', media_type='chat')
            else:
                contactWindow.historyViewer.filterByContact(format_identity(sessionController.target_uri), media_type='chat')

def validateToolbarButtonWhileDisconnected(sessionController, item):
    settings = SIPSimpleSettings()
    valid_items = []

    if NSApp.delegate().applicationName != 'Blink Lite':
        valid_items.append('history')
        valid_items.append(NSToolbarPrintItemIdentifier)

    valid_items.append('connect_button')
    valid_items.append('smileys')
    if sessionController.account is not BonjourAccount() and not settings.chat.disable_collaboration_editor:
        valid_items.append('editor')

    return item.itemIdentifier() in valid_items

def updateToolbarButtonsWhileDisconnected(sessionController, toolbar):
    settings = SIPSimpleSettings()
    for item in toolbar.visibleItems():
        identifier = item.itemIdentifier()
        if identifier == 'connect_button':
            item.setEnabled_(True if sessionController.account is not BonjourAccount() else False)
            item.setToolTip_('Click to start a chat session')
            item.setLabel_(u'Connect')
            item.setImage_(NSImage.imageNamed_("start_chat"))
        elif identifier == 'audio':
            item.setToolTip_('Click to add audio to this session')
            item.setImage_(NSImage.imageNamed_("audio"))
        elif identifier == 'record':
            item.setImage_(NSImage.imageNamed_("record"))
        elif identifier == 'hold':
            item.setImage_(NSImage.imageNamed_("pause"))
        elif identifier == 'video':
            item.setImage_(NSImage.imageNamed_("video"))
        elif identifier == 'desktop':
            item.setEnabled_(False)
        elif identifier == 'screenshot':
            item.setEnabled_(False)
        elif identifier == 'sendfile':
            item.setEnabled_(False)

kCGWindowListOptionOnScreenOnly = 1 << 0
kCGNullWindowID = 0
kCGWindowImageDefault = 0


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
    quality_settings = {'low':    {'compression': 0.3, 'width': 800,  'framerate': 1},
                        'medium': {'compression': 0.5, 'width': 1024, 'framerate': 1},
                        'high':   {'compression': 0.7, 'width': None, 'framerate': 1}
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
            NotificationCenter().remove_observer(self, sender=self.stream)
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

            if self.show_preview:
                ScreensharingPreviewPanel(image)
                self.show_preview = False

            jpeg = NSBitmapImageRep.alloc().initWithData_(image.TIFFRepresentation()).representationUsingType_properties_(NSJPEGFileType, {NSImageCompressionFactor: self.compression})
            # this also works and produces the same result, but it's not documented anywhere
            #jpeg = image.IKIPJPEGDataWithMaxSize_compression_(image.size().width, self.compression)

            if self.log_first_frame:
                BlinkLogger().log_info('Sending %s bytes %s width screen' % (len(jpeg), image.size().width))
                self.log_first_frame = False
            self.may_send = False
            self.stream.send_message(str(jpeg), content_type='application/blink-screensharing', timestamp=Timestamp(datetime.datetime.now(tzlocal())))


class MessageInfo(object):
    def __init__(self, msgid, direction='outgoing', sender=None, recipient=None, timestamp=None, text=None, private=False, status=None):
        self.msgid = msgid 
        self.direction = direction
        self.sender = sender
        self.recipient = recipient
        self.timestamp = timestamp
        self.text = text
        self.private = private
        self.status = status


class MessageHandler(NSObject):
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

    session = None
    stream = None
    connected = False
    delegate = None
    messages = None
    pending = None
    remote_uri = None
    local_uri = None

    def initWithSession_(self, session):
        self = super(MessageHandler, self).init()
        if self:
            self.session = session
            self.stream = None
            self.connected = None
            self.messages = {}
            self.pending = []
            self.history = ChatHistory()
        return self

    def setDelegate(self, delegate):
        self.delegate = delegate
        self.local_uri = '%s@%s' % (self.delegate.account.id.username, self.delegate.account.id.domain) if self.delegate.account is not BonjourAccount() else 'bonjour'
        self.remote_uri = format_identity_address(self.delegate.delegate.sessionController.remotePartyObject)

    def _send(self, msgid):
        message = self.messages.pop(msgid)
        if message.private and message.recipient is not None:
            try:
                id = self.stream.send_message(message.text, timestamp=message.timestamp, recipients=[message.recipient])
            except ChatStreamError, e:
                BlinkLogger().log_error(u"Error sending message: %s" % e)
                self.delegate.markMessage(msgid, MSG_STATE_FAILED, private)
                message.status='failed'
                self.add_to_history(message)
                return False
        else:
            try:
                id = self.stream.send_message(message.text, timestamp=message.timestamp)
            except ChatStreamError, e:
                BlinkLogger().log_error(u"Error sending message: %s" % e)
                self.delegate.markMessage(msgid, MSG_STATE_FAILED, private)
                message.status='failed'
                self.add_to_history(message)
                return False

        message.status = "sent"
        self.messages[id] = message

        return True

    def send(self, text, recipient=None, private=False):
        now = datetime.datetime.now(tzlocal())
        timestamp = Timestamp(now)
        icon = NSApp.delegate().windowController.iconPathForSelf()
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
                    self._send(msgid)
                except Exception, e:
                    BlinkLogger().log_error(u"Error sending message: %s" % e)
                    self.delegate.showSystemMessage("Error sending message",now, True)
                else:
                    self.delegate.showMessage(msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="sent", recipient=recipient_html)
            else:
                self.pending.append(msgid)
                self.delegate.showMessage(msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="queued", recipient=recipient_html)

        return True

    def resend(self, msgid, text, recipient=None, private=False):
        now = datetime.datetime.now(tzlocal())
        timestamp = Timestamp(now)
        recipient_html = "%s <%s@%s>" % (recipient.display_name, recipient.uri.user, recipient.uri.host) if recipient else ''
        icon = NSApp.delegate().windowController.iconPathForSelf()

        self.messages[msgid] = MessageInfo(msgid=msgid, recipient=recipient, timestamp=timestamp, text=text, private=private, status="queued")

        if self.connected:
            try:
                self._send(msgid)
            except Exception, e:
                BlinkLogger().log_error(u"Error sending message: %s" % e)
                self.delegate.showSystemMessage("Error sending message",now, True)
            else:
                self.delegate.showMessage(msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="sent", recipient=recipient_html)
        else:
            self.pending.append(msgid)
            self.delegate.showMessage(msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="queued", recipient=recipient_html)

    def setConnected(self, stream):
        self.connected = True
        self.stream = stream
        NotificationCenter().add_observer(self, sender=stream)
        for msgid in self.pending:
            private = self.messages[msgid].private
            sent = self._send(msgid)
            if not sent:
                BlinkLogger().log_error(u"Error sending queued message: %s" % msgid)
            else:
                self.delegate.markMessage(msgid, MSG_STATE_SENDING, private)
        self.pending = []

    def setDisconnected(self):
        self.connected = False
        for msgid in self.pending:
            message = self.messages.pop(msgid)
            message.status='failed'
            self.delegate.markMessage(msgid, MSG_STATE_FAILED)
            self.add_to_history(message)
        if self.stream:
            NotificationCenter().remove_observer(self, sender=self.stream)
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
                message.status='delivered'
                self.markMessage(message, MSG_STATE_DELIVERED)
                self.add_to_history(message)
                self.lastDeliveredTime = time.time()
        except KeyError:
            pass

    def _NH_ChatStreamDidNotDeliverMessage(self, sender, data):
        try:
            message = self.messages.pop(data.message_id)
            if message:
                message.status='failed'
                self.markMessage(message, MSG_STATE_FAILED)
                self.add_to_history(message)
        except KeyError:
            pass

    @allocate_autorelease_pool
    @run_in_green_thread
    def add_to_history(self, message):
        # writes the record to the sql database
        cpim_to = "%s <%s@%s>" % (message.recipient.display_name, message.recipient.uri.user, message.recipient.uri.host) if message.recipient else ''
        cpim_from = format_identity(message.sender) if message.sender else ''
        cpim_timestamp = str(message.timestamp)
        private = "1" if message.private else "0"

        try:
            self.history.add_message(message.msgid, 'chat', self.local_uri, self.remote_uri, message.direction, cpim_from, cpim_to, cpim_timestamp, message.text, "text", private, message.status)
        except Exception, e:
            BlinkLogger().log_error(u"Failed to add message to history: %s" % e)


class ChatController(MediaStream):
    implements(IObserver)

    chatViewController = objc.IBOutlet()
    smileyButton = objc.IBOutlet()

    addContactView = objc.IBOutlet()
    addContactLabel = objc.IBOutlet()

    splitView = objc.IBOutlet()
    splitViewFrame = None
    video_frame_visible = False

    videoContainer = objc.IBOutlet()
    inputContainer = objc.IBOutlet()
    outputContainer = objc.IBOutlet()

    fullScreenVideoPanel = objc.IBOutlet()
    fullScreenVideoPanelToobar = objc.IBOutlet()
    
    document = None
    fail_reason = None
    sessionController = None
    stream = None
    finishedLoading = False
    showHistoryEntries = 20

    history = None
    handler = None
    screensharing_handler = None

    lastDeliveredTime = None
    undeliveredMessages = {} # id -> message

    # timer is reset whenever remote end sends is-composing active, when it times out, go to idle
    remoteTypingTimer = None

    drawerSplitterPosition = None
    mainViewSplitterPosition = None

    screenshot_task = None

    @classmethod
    def createStream(self, account):
        return ChatStream(account)

    def initWithOwner_stream_(self, scontroller, stream):
        self = super(ChatController, self).initWithOwner_stream_(scontroller, stream)
        self.mediastream_failed = False
        self.session_failed = False
        self.share_screen_in_conference = False
        self.last_failure_reason = None

        if self:
            self.history_msgid_list=set()

            self.remote_uri = format_identity_address(self.sessionController.remotePartyObject)
            self.local_uri = '%s@%s' % (self.sessionController.account.id.username, self.sessionController.account.id.domain) if self.sessionController.account is not BonjourAccount() else 'bonjour'

            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, sender=stream)
            self.notification_center.add_observer(self, sender=self.sessionController)
            self.notification_center.add_observer(self, name='BlinkFileTransferDidEnd')
            self.notification_center.add_observer(self, name='BlinkMuteChangedState')

            NSBundle.loadNibNamed_owner_("ChatView", self)

            self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("ChatView", "html"))

            self.chatViewController.setAccount_(self.sessionController.account)
            self.chatViewController.resetRenderedMessages()

            self.handler = MessageHandler.alloc().initWithSession_(self.sessionController.session)
            self.handler.setDelegate(self.chatViewController)
            self.screensharing_handler = ConferenceScreenSharingHandler()
            self.screensharing_handler.setDelegate(self)

            self.history=ChatHistory()
            self.backend = SIPManager()

        return self

    @property
    def screensharing_allowed(self):
        return 'com.ag-projects.screen-sharing' in chain(*(attr.split() for attr in self.stream.remote_media.attributes.getall('chatroom')))

    def getWindow(self):
        return ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)

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

    def dealloc(self):
        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
        
        super(ChatController, self).dealloc()

    def getContentView(self):
        return self.chatViewController.view

    def insertSmiley_(self, sender):
        smiley = sender.representedObject()
        self.chatViewController.appendAttributedString_(smiley)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def changeStatus(self, newstate, fail_reason=None):
        self.status = newstate
        MediaStream.changeStatus(self, newstate, fail_reason)

    def openChatWindow(self):
        ChatWindowManager.ChatWindowManager().addChatWindow(self.sessionController)
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        window.chat_controllers.add(self)
        window.drawer.open()
        self.changeStatus(STREAM_IDLE)

    def startOutgoing(self, is_update):
        self.openChatWindow()
        if is_update and self.sessionController.canProposeMediaStreamChanges():
            self.changeStatus(STREAM_PROPOSING)
        else:
            self.changeStatus(STREAM_WAITING_DNS_LOOKUP)

    def startIncoming(self, is_update):
        ChatWindowManager.ChatWindowManager().addChatWindow(self.sessionController)
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        window.chat_controllers.add(self)
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_INCOMING)

    def sendFiles(self, fnames):
        ws = NSWorkspace.sharedWorkspace()
        filenames = [unicodedata.normalize('NFC', file) for file in fnames if os.path.isfile(file)]
        if filenames:
            self.backend.send_files_to_contact(self.sessionController.account, self.sessionController.target_uri, filenames)
            return True
        return False

    def validateToolbarItem_(self, item):
        return True

    @objc.IBAction
    def userClickedFullScreenToolbarButton_(self, sender):
        if sender.itemIdentifier() == 'hangup':
            self.closeTab()
        elif sender.itemIdentifier() == 'mirror':
            self.toggleVideoMirror()
        elif sender.itemIdentifier() == 'mute':
            self.backend.mute(False if self.backend.is_muted() else True)
            self.notification_center.post_notification("BlinkMuteChangedState", sender=self, data=TimestampedNotificationData())
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
            window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
            if window:
                window.window().performZoom_(None)
        elif sender.itemIdentifier() == 'exit':
            self.exitFullScreen()

    @objc.IBAction
    def addContactPanelClicked_(self, sender):
        if sender.tag() == 1:
            NSApp.delegate().windowController.addContact(self.sessionController.target_uri)
        self.addContactView.removeFromSuperview()
        frame = self.chatViewController.outputView.frame()
        frame.origin.y = 0
        frame.size = self.outputContainer.frame().size
        self.chatViewController.outputView.setFrame_(frame)
    
    def enableAddContactPanel(self):
        text = u"%s is not in your Contacts List. Would you like to add it now?" % self.sessionController.getTitleShort()
        self.addContactLabel.setStringValue_(text)
        frame = self.chatViewController.outputView.frame()
        frame.size.height -= NSHeight(self.addContactView.frame())
        frame.origin.y += NSHeight(self.addContactView.frame())
        self.chatViewController.outputView.setFrame_(frame)
        self.outputContainer.addSubview_(self.addContactView)
        frame = self.addContactView.frame()
        frame.origin = NSZeroPoint
        self.addContactView.setFrame_(frame)

    def textView_doCommandBySelector_(self, textView, selector):
        if selector == "insertNewline:" and self.chatViewController.inputText == textView:
            original = textView.string()
            text = unicode(original)
            textView.setString_("")
            if text:
                recipient = '%s <sip:%s>' % (self.sessionController.contactDisplayName, format_identity_address(self.sessionController.remotePartyObject))
                try:
                    identity = CPIMIdentity.parse(recipient)
                except ValueError:
                    identity = None
                if not self.handler.send(text, recipient=identity):
                    textView.setString_(original)
                NotificationCenter().post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=TimestampedNotificationData(direction='outgoing', history_entry=False, remote_party=format_identity(self.sessionController.remotePartyObject), local_party=format_identity_address(self.sessionController.account) if self.sessionController.account is not BonjourAccount() else 'bonjour', check_contact=True))

            if not self.stream or self.status in [STREAM_FAILED, STREAM_IDLE]:
                BlinkLogger().log_info(u"Session not established, starting it")
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
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if not window:
            return

        window.drawer.open()

        self.splitViewFrame = window.window().frame()

        self.saveSplitterPosition()

        self.splitView.setDividerStyle_(NSSplitViewDividerStyleThin)
        output_frame = self.outputContainer.frame()
        output_frame.size.height = 0
        self.outputContainer.setFrame_(output_frame)
        input_frame = self.outputContainer.frame()
        input_frame.size.height = 0
        self.inputContainer.setFrame_(input_frame)

        # Hide Dock and other desktop items
        SetSystemUIMode(kUIModeAllHidden, 0)

        window.window().makeFirstResponder_(self.videoContainer)

        window.window().setMovableByWindowBackground_(True)
        fullframe = NSScreen.mainScreen().frame()
        fullframe.size.height += 20
        window.window().setFrame_display_animate_(fullframe, True, True)
        window.window().setMovable_(False)

        self.notification_center.post_notification("BlinkVideoEnteredFullScreen", sender=self, data=TimestampedNotificationData())
       
        self.showFullScreenVideoPanel()
        self.showVideoMirror()

        window.window().setInitialFirstResponder_(self.videoContainer)

    def exitFullScreen(self):
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if not window:
            return

        self.hideVideoMirror()

        if self.splitViewFrame:
            window.window().setFrame_display_(self.splitViewFrame, True)
            window.window().setMovable_(True)

            self.splitView.setDividerStyle_(NSSplitViewDividerStyleThin)
            self.restoreSplitterPosition()
            self.splitViewFrame = None

        if self.fullScreenVideoPanel:
            self.fullScreenVideoPanel.orderOut_(self)

        # Restore Dock and other desktop items
        SetSystemUIMode(kUIModeNormal, kUIOptionAutoShowMenuBar)

        self.notification_center.post_notification("BlinkVideoExitedFullScreen", sender=self, data=TimestampedNotificationData())

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
        NSApp.delegate().windowController.mirrorWindow.show()

    def hideVideoMirror(self):
        NSApp.delegate().windowController.mirrorWindow.hide()

    def toggleVideoMirror(self):
        if NSApp.delegate().windowController.mirrorWindow.visible:
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

            new_output_height = 300 if self.chatViewController.editorStatus else 0

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
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if not window:
            return

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

            self.video_frame_visible = True

        else:
            window.drawer.open()
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

            self.video_frame_visible = False

    @run_in_green_thread
    @allocate_autorelease_pool
    def replay_history(self):
        if self.sessionController.account is BonjourAccount():
            return
        try:
            results = self.history.get_messages(local_uri=self.local_uri, remote_uri=self.remote_uri, media_type='chat', count=self.showHistoryEntries)
        except Exception, e:
            self.sessionController.log_info(u"Failed to retrive chat history for %s: %s" % (self.remote_uri, e))            
            return

        # build a list of previously failed messages
        last_failed_messages=[]
        for row in results:
            if row.status == 'delivered':
                break
            last_failed_messages.append(row)    
        last_failed_messages.reverse()

        self.history_msgid_list = [row.msgid for row in reversed(list(results))]

        # render last delievered messages except those due to be resent
        messages_to_render = [row for row in reversed(list(results)) if row.msgid not in last_failed_messages]
        self.render_history_messages(messages_to_render)

        self.resend_last_failed_message(last_failed_messages)
            
    @allocate_autorelease_pool
    @run_in_gui_thread
    def render_history_messages(self, messages):
        for message in messages: 
            if message.direction == 'outgoing':
                icon = NSApp.delegate().windowController.iconPathForSelf()
            else:
                sender_uri = format_identity_from_text(message.cpim_from)[0]
                icon = NSApp.delegate().windowController.iconPathForURI(sender_uri)

            timestamp=Timestamp.parse(message.cpim_timestamp)
            is_html = False if message.content_type == 'text' else True
            private = True if message.private == "1" else False

            self.chatViewController.showMessage(message.msgid, message.direction, message.cpim_from, icon, message.body, timestamp, is_private=private, recipient=message.cpim_to, state=message.status, is_html=is_html, history_entry=True)
                            
    @allocate_autorelease_pool
    @run_in_gui_thread
    def resend_last_failed_message(self, messages):
        if self.sessionController.account is BonjourAccount():
            return

        for message in messages:
            if message.cpim_to:
                address, display_name, full_uri, fancy_uri = format_identity_from_text(message.cpim_to)
                try:
                    recipient = CPIMIdentity.parse('%s <sip:%s>' % (display_name, address))
                except ValueError:
                    continue
            else:
                recipient = None

            private = True if message.private == "1" else False
            self.handler.resend(message.msgid, message.body, recipient, private)    

    def chatViewDidGetNewMessage_(self, chatView):
        NSApp.delegate().noteNewMessage(self.chatViewController.outputView.window())
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if window:
            window.noteNewMessageForSession_(self.sessionController)

    def updateToolbarButtons(self, toolbar, got_proposal=False):
        """Called by ChatWindowController when receiving various middleware notifications"""
        settings = SIPSimpleSettings()

        if self.sessionController.hasStreamOfType("audio"):
            audio_stream = self.sessionController.streamHandlerOfType("audio")

        for item in toolbar.visibleItems():
            identifier = item.itemIdentifier()
            if identifier == 'connect_button':
                if self.status in (STREAM_CONNECTING, STREAM_PROPOSING, STREAM_WAITING_DNS_LOOKUP):
                    item.setToolTip_('Click to cancel the chat session')
                    item.setLabel_(u'Cancel')
                    item.setImage_(NSImage.imageNamed_("stop_chat"))
                elif self.status == STREAM_CONNECTED:
                    item.setToolTip_('Click to stop the chat session')
                    item.setLabel_(u'Disconnect')
                    item.setImage_(NSImage.imageNamed_("stop_chat"))
                else:
                    item.setToolTip_('Click to start a chat session')
                    item.setLabel_(u'Connect')
                    item.setImage_(NSImage.imageNamed_("start_chat"))
            elif identifier == 'audio':
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_CONNECTED:
                        item.setToolTip_('Click to hangup the audio call')
                        item.setImage_(NSImage.imageNamed_("hangup"))
                    elif audio_stream.status in (STREAM_PROPOSING, STREAM_RINGING):
                        item.setToolTip_('Click to cancel the audio call')
                        item.setImage_(NSImage.imageNamed_("hangup"))
                else:
                    item.setToolTip_('Click to add audio to this session')
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
                item.setImage_(NSImage.imageNamed_("record" if self.sessionController.hasStreamOfType("audio") and audio_stream.status == STREAM_CONNECTED and audio_stream.stream.recording_active else "recording1"))
            elif identifier == 'video' and self.backend.isMediaTypeSupported('video'):
                if self.sessionController.hasStreamOfType("video"):
                    video_stream = self.sessionController.streamHandlerOfType("video")
                    if video_stream.status == STREAM_PROPOSING or video_stream.status == STREAM_RINGING:
                        item.setToolTip_('Click to cancel the video call')
                        item.setImage_(NSImage.imageNamed_("hangup"))
                    elif video_stream.status == STREAM_CONNECTED:
                        item.setToolTip_('Click to hangup the video call')
                        item.setImage_(NSImage.imageNamed_("hangup"))
                else:
                    item.setToolTip_('Click to add video to this session')
                    item.setImage_(NSImage.imageNamed_("video"))
            elif identifier == 'desktop':
                if not self.sessionController.remote_focus:
                    item.setEnabled_(True if self.status == STREAM_CONNECTED else False)
                    menu = toolbar.delegate().desktopShareMenu
                    item.setImage_(NSImage.imageNamed_("display"))

                    title = self.sessionController.getTitleShort()
                    mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_REQUEST_REMOTE)
                    mitem.setTitle_("Request Screen from %s" % title)
                    mitem.setHidden_(False)

                    mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL)
                    mitem.setTitle_("Share My Screen with %s" % title)
                    mitem.setHidden_(False)

                    mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_CANCEL)
                    mitem.setHidden_(False)

                    if self.sessionController.hasStreamOfType("desktop-sharing"):
                        desktop_sharing_stream = self.sessionController.streamHandlerOfType("desktop-sharing")
                        if desktop_sharing_stream.status == STREAM_PROPOSING or desktop_sharing_stream.status == STREAM_RINGING:
                            mitem.setTitle_("Cancel Screen Sharing Proposal")
                        elif desktop_sharing_stream.status == STREAM_CONNECTED:
                            mitem.setTitle_("Stop Screen Sharing")
                    else:
                        mitem.setTitle_("Cancel Screen Sharing Proposal")
                else:
                    item.setEnabled_(True if self.status == STREAM_CONNECTED and self.screensharing_allowed else False)
                    item.setImage_(NSImage.imageNamed_("display_red" if self.share_screen_in_conference else "display"))

            elif identifier == 'smileys':
                item.setImage_(NSImage.imageNamed_("smiley_on" if self.chatViewController.expandSmileys else "smiley_off"))
                item.setEnabled_(True)
            elif identifier == 'editor' and self.sessionController.account is not BonjourAccount() and not settings.chat.disable_collaboration_editor:
                item.setImage_(NSImage.imageNamed_("editor-changed" if not self.chatViewController.editorStatus and self.chatViewController.editor_has_changed else "editor"))
            elif identifier == 'screenshot':
                item.setEnabled_(True if self.status == STREAM_CONNECTED and self.backend.isMediaTypeSupported('file-transfer') else False)
            elif identifier == 'sendfile':
                item.setEnabled_(True if self.status == STREAM_CONNECTED and self.backend.isMediaTypeSupported('file-transfer') else False)

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

            if identifier == 'connect_button':
                if self.status in (STREAM_CONNECTING, STREAM_PROPOSING, STREAM_WAITING_DNS_LOOKUP):
                    return True if self.sessionController.canCancelProposal() else False
                elif self.status == STREAM_CONNECTED:
                    return True if self.sessionController.canProposeMediaStreamChanges() else False
                else:
                    return True if self.sessionController.canProposeMediaStreamChanges() else False
            elif identifier == 'audio' and self.status == STREAM_CONNECTED:
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_CONNECTED:
                        return True if self.sessionController.canProposeMediaStreamChanges() else False
                    elif audio_stream.status in (STREAM_PROPOSING, STREAM_RINGING):
                        return True if self.sessionController.canCancelProposal() else False
                    else:
                        return True if self.sessionController.canProposeMediaStreamChanges() else False
                else:
                    return True if self.sessionController.canProposeMediaStreamChanges() else False
            elif identifier == 'hold' and self.status == STREAM_CONNECTED:
                if self.sessionController.hasStreamOfType("audio") and audio_stream.status == STREAM_CONNECTED:
                    return True
            elif identifier == 'record':
                return True if self.sessionController.hasStreamOfType("audio") and audio_stream.status == STREAM_CONNECTED and NSApp.delegate().applicationName != 'Blink Lite' else False
            elif identifier == 'maximize' and self.video_frame_visible:
                return True
            elif identifier == 'video' and self.status == STREAM_CONNECTED and self.backend.isMediaTypeSupported('video'):
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
            elif identifier == 'sendfile' and self.backend.isMediaTypeSupported('file-transfer') and self.status == STREAM_CONNECTED:
                return True
            elif identifier == 'smileys':
                return True
            elif identifier == 'editor' and self.sessionController.account is not BonjourAccount():
                settings = SIPSimpleSettings()
                if not settings.chat.disable_collaboration_editor:
                    return True
            elif identifier == 'history' and NSApp.delegate().applicationName != 'Blink Lite':
                return True
        elif item.tag() == TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL:
            if not self.sessionController.remote_focus:
                return True if self.sessionController.canProposeMediaStreamChanges() and not self.sessionController.hasStreamOfType("desktop-sharing") else False
            elif self.screensharing_allowed:
                return True
        elif item.tag() == TOOLBAR_SCREENSHARING_MENU_REQUEST_REMOTE:
            if not self.sessionController.remote_focus:
                return True if self.sessionController.canProposeMediaStreamChanges() and not self.sessionController.hasStreamOfType("desktop-sharing") else False
        elif item.tag() == TOOLBAR_SCREENSHARING_MENU_CANCEL:
            if self.sessionController.hasStreamOfType("desktop-sharing"):
                desktop_sharing_stream = self.sessionController.streamHandlerOfType("desktop-sharing")
                if desktop_sharing_stream.status == STREAM_PROPOSING or desktop_sharing_stream.status == STREAM_RINGING:
                    return True if self.sessionController.canCancelProposal() else False
                elif desktop_sharing_stream.status == STREAM_CONNECTED:
                    return True if self.sessionController.canProposeMediaStreamChanges() else False
        elif item.tag() in (TOOLBAR_SCREENSHOT_WINDOW_MENU, TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU, TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_HIGH, TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_LOW, TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_MEDIUM):
            if self.sessionController.remote_focus and self.screensharing_handler.connected:
                return True

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

            if identifier == 'audio' and self.status == STREAM_CONNECTED:
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_PROPOSING or audio_stream.status == STREAM_RINGING:
                        self.sessionController.cancelProposal(audio_stream)
                    else:
                        self.sessionController.removeAudioFromSession()

                    sender.setToolTip_('Click to add audio to this session')
                    sender.setImage_(NSImage.imageNamed_("audio"))

                    # The button will be enabled again after operation is finished
                    sender.setEnabled_(False)
                else:
                    self.sessionController.addAudioToSession()
                    sender.setToolTip_('Click to cancel the audio call')
                    sender.setImage_(NSImage.imageNamed_("hangup"))
                    self.notification_center.post_notification("SIPSessionGotRingIndication", sender=self.sessionController.session, data=TimestampedNotificationData())

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

            elif identifier == 'hold' and self.status == STREAM_CONNECTED and self.sessionController.hasStreamOfType("audio") and not self.sessionController.inProposal:
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

                        sender.setToolTip_('Click to add video to this session')

                        # The button will be enabled again after operation is finished
                        sender.setEnabled_(False)
                    else:
                        self.sessionController.addVideoToSession()
                        sender.setToolTip_('Click to cancel the video call')

            elif identifier == 'maximize':
                self.enterFullScreen()

            elif identifier == 'sendfile':
                openFileTransferSelectionDialog(self.sessionController.account, self.sessionController.target_uri)

            elif identifier == 'connect_button':
                if self.status in (STREAM_IDLE, STREAM_FAILED):
                    if self.sessionController.canProposeMediaStreamChanges():               
                        self.sessionController.log_info(u"Re-establishing session to %s" % self.remoteParty)
                        self.sessionController.mustShowDrawer = True
                        self.sessionController.startChatSession()
                    else:
                        BlinkLogger().log_info(u"Session is pending a proposal")

                elif self.status in (STREAM_CONNECTED, STREAM_CONNECTING, STREAM_PROPOSING, STREAM_WAITING_DNS_LOOKUP):
                    self.endStream()
                
            elif identifier == 'smileys':
                self.chatViewController.expandSmileys = not self.chatViewController.expandSmileys
                sender.setImage_(NSImage.imageNamed_("smiley_on" if self.chatViewController.expandSmileys else "smiley_off"))
                self.chatViewController.toggleSmileys(self.chatViewController.expandSmileys)

            elif identifier == 'editor' and self.sessionController.account is not BonjourAccount() and not settings.chat.disable_collaboration_editor:
                sender.setImage_(NSImage.imageNamed_("editor"))
                sender.setToolTip_("Switch to Chat Session" if self.chatViewController.editorStatus else "Enable Collaborative Editor")
                self.toggleEditor()
            elif identifier == 'history' and NSApp.delegate().applicationName != 'Blink Lite':
                contactWindow = self.sessionController.owner
                contactWindow.showHistoryViewer_(None)
                if self.sessionController.account is BonjourAccount():
                    contactWindow.historyViewer.filterByContact('bonjour', media_type='chat')
                else:
                    contactWindow.historyViewer.filterByContact(format_identity(self.sessionController.target_uri), media_type='chat')

        elif sender.tag() in (TOOLBAR_SCREENSHOT_MENU_WINDOW, TOOLBAR_SCREENSHOT_MENU_AREA):
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

        elif sender.tag() == TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL and self.status == STREAM_CONNECTED:
            if not self.sessionController.remote_focus:
                if not self.sessionController.hasStreamOfType("desktop-sharing"):
                    self.sessionController.addMyDesktopToSession()
                    sender.setEnabled_(False)
            else:
                self.toggleScreensharingWithConferenceParticipants()
        elif sender.tag() == TOOLBAR_SCREENSHARING_MENU_REQUEST_REMOTE and self.status == STREAM_CONNECTED:
            if not self.sessionController.hasStreamOfType("desktop-sharing"):
                self.sessionController.addRemoteDesktopToSession()
                sender.setEnabled_(False)
        elif sender.tag() == TOOLBAR_SCREENSHARING_MENU_CANCEL and self.status == STREAM_CONNECTED:
            if self.sessionController.hasStreamOfType("desktop-sharing"):
                desktop_sharing_stream = self.sessionController.streamHandlerOfType("desktop-sharing")
                if desktop_sharing_stream.status == STREAM_PROPOSING or desktop_sharing_stream.status == STREAM_RINGING:
                    self.sessionController.cancelProposal(desktop_sharing_stream)
                elif desktop_sharing_stream.status == STREAM_CONNECTED:
                    self.sessionController.removeDesktopFromSession()
        elif sender.tag() == TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_HIGH:
            if self.screensharing_handler.connected:
                self.screensharing_handler.setQuality('high')
        elif sender.tag() == TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_LOW:
            if self.screensharing_handler.connected:
                self.screensharing_handler.setQuality('low')
        elif sender.tag() == TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_MEDIUM:
            if self.screensharing_handler.connected:
                self.screensharing_handler.setQuality('medium')

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
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if window:
            try:
                item = (item for item in window.toolbar.visibleItems() if item.tag() == TOOLBAR_SCREENSHARING_BUTTON).next()
            except StopIteration:
                pass
            else:
                item.setImage_(NSImage.imageNamed_("display_red" if self.share_screen_in_conference else "display"))

            menu = window.toolbar.delegate().conferenceScreeningSharingMenu
            menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL).setTitle_("Share My Screen with Conference Participants" if self.share_screen_in_conference == False else "Stop Screen Sharing")
            window.noteSession_isScreenSharing_(self.sessionController, self.share_screen_in_conference)

    def resetEditorToolbarIcon(self):
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if window:
            try:
                item = (item for item in window.toolbar.visibleItems() if item.tag() == 109).next()
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
        self.chatViewController.editor_has_changed = False
        self.chatViewController.editorStatus = not self.chatViewController.editorStatus
        self.showChatViewWithEditorWhileVideoActive()
        self.chatViewController.toggleCollaborationEditor(self.chatViewController.editorStatus)
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if window:
            window.noteSession_isComposing_(self.sessionController, False)

    def remoteBecameIdle_(self, timer):
        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
        self.remoteTypingTimer = None
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if window:
            window.noteSession_isComposing_(self.sessionController, False)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_ChatStreamGotMessage(self, stream, data):
        message = data.message
        if not message.content_type.startswith("text/"):
            return

        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if not window:
            return

        hash = hashlib.sha1()
        hash.update(message.body.encode("utf-8")+str(message.timestamp))
        msgid = hash.hexdigest()

        if msgid not in self.history_msgid_list:
            sender = message.sender
            recipient = message.recipients[0]
            private = data.private
            text = message.body
            timestamp = message.timestamp
            if window:
                name = format_identity(sender)
                icon = NSApp.delegate().windowController.iconPathForURI(format_identity_address(sender))
                recipient_html = '%s <%s@%s>' % (recipient.display_name, recipient.uri.user, recipient.uri.host) if recipient else ''
                self.chatViewController.showMessage(msgid, 'incoming', name, icon, text, timestamp, is_private=private, recipient=recipient_html, state="delivered")

                tab = self.chatViewController.outputView.window()
                tab_is_key = tab.isKeyWindow() if tab else False

                # FancyTabViewSwitcher will set unfocused tab item views as Hidden
                if not tab_is_key or self.chatViewController.view.isHiddenOrHasHiddenAncestor():
                    # notify growl
                    growl_data = TimestampedNotificationData()
                    growl_data.sender = format_identity_simple(sender)
                    growl_data.content = html2txt(message.body[0:400]) if message.content_type == 'text/html' else message.body[0:400]
                    NotificationCenter().post_notification("GrowlGotChatMessage", sender=self, data=growl_data)

                NotificationCenter().post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity(self.sessionController.remotePartyObject), local_party=format_identity_address(self.sessionController.account) if self.sessionController.account is not BonjourAccount() else 'bonjour', check_contact=True))

            # save to history
            message = MessageInfo(msgid, direction='incoming', sender=sender, recipient=recipient, timestamp=timestamp, text=text, private=private, status="delivered")
            self.handler.add_to_history(message)

    def _NH_ChatStreamGotComposingIndication(self, stream, data):
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if not window:
            return
        flag = data.state == "active"
        if flag:
            refresh = data.refresh if data.refresh is not None else 120

            if data.last_active is not None and (data.last_active - datetime.datetime.now(tzlocal()) > datetime.timedelta(seconds=refresh)):
                # message is old, discard it
                return

            self.resetIsComposingTimer(refresh)
        else:
            if self.remoteTypingTimer:
                self.remoteTypingTimer.invalidate()
                self.remoteTypingTimer = None

        window.noteSession_isComposing_(self.sessionController, flag)

    def _NH_BlinkMuteChangedState(self, sender, data):
        self.updateToolbarMuteIcon()

    def _NH_BlinkFileTransferDidEnd(self, sender, data):
        if self.sessionController.session is None:
            return

        settings = SIPSimpleSettings()
        if not settings.file_transfer.render_incoming_image_in_chat_window and not settings.file_transfer.render_incoming_video_in_chat_window:
            return

        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if not window or self.sessionController.remoteSIPAddress != sender.remote_identity:
            NSApp.delegate().windowController.fileTransfersWindow.showWindow_(None)
            return

        if image_file_extension_pattern.search(data.file_path):
            text  = "Incoming image file transfer has finished"
            try:
                image = NSImage.alloc().initWithContentsOfFile_(data.file_path)
                w = image.size().width
                width = w if w and w < 600 else '100%'
            except:
                width = '100%'

            text += "<p><img src='%s' border='0' width='%s'>" % (data.file_path, width)
        else:
            return

        name = format_identity(self.sessionController.session.remote_identity)
        icon = NSApp.delegate().windowController.iconPathForURI(format_identity_address(self.sessionController.session.remote_identity))
        now = datetime.datetime.now(tzlocal())
        timestamp = Timestamp(now)
        self.chatViewController.showMessage(str(uuid.uuid1()), 'incoming', name, icon, text, timestamp, state="delivered", history_entry=True, is_html=True)

    def _NH_BlinkSessionDidFail(self, sender, data):
        self.session_failed = True
        if not self.mediastream_failed:
            reason = data.failure_reason or data.reason
            if reason != 'Session Cancelled':
                if self.last_failure_reason != reason:
                    self.last_failure_reason = reason
                    message = "Session failed (%s): %s" % (data.originator, reason)
                    self.chatViewController.showSystemMessage(message, datetime.datetime.now(tzlocal()), True)
        self.changeStatus(STREAM_FAILED)
        self.notification_center.remove_observer(self, sender=sender)
        self.notification_center.remove_observer(self, name='BlinkFileTransferDidEnd')

    def _NH_BlinkSessionDidStart(self, sender, data):
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
            self.chatViewController.showSystemMessage(message, datetime.datetime.now(tzlocal()), True)

    def _NH_BlinkProposalGotRejected(self, sender, data):
        if data.code != 487:
            if self.last_failure_reason != data.reason:
                self.last_failure_reason = data.reason
                message = "Proposal rejected: %s" % data.reason if data.code != 200 else "Proposal rejected"
                self.chatViewController.showSystemMessage(message, datetime.datetime.now(tzlocal()), True)

    def _NH_BlinkSessionDidEnd(self, sender, data):
        self.notification_center.remove_observer(self, sender=sender)
        self.notification_center.remove_observer(self, name='BlinkFileTransferDidEnd')

    def _NH_MediaStreamDidStart(self, sender, data):
        self.last_failure_reason = None
        endpoint = str(self.stream.msrp.full_remote_path[0])
        self.sessionController.log_info(u"Chat stream established to %s" % endpoint)
        self.chatViewController.showSystemMessage("Session established", datetime.datetime.now(tzlocal()))

        self.handler.setConnected(self.stream)

        # needed to set the Audio button state after session has started
        self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self, data=TimestampedNotificationData())

        self.changeStatus(STREAM_CONNECTED)

    def _NH_MediaStreamDidEnd(self, sender, data):
        self.sessionController.log_info(u"Chat stream ended")
        if not self.session_failed and not self.mediastream_failed:
            close_message = "%s has left the conversation" % self.sessionController.getTitleShort()
            self.chatViewController.showSystemMessage(close_message, datetime.datetime.now(tzlocal()))

        self.changeStatus(STREAM_IDLE, self.sessionController.endingBy)
        self.notification_center.remove_observer(self, sender=sender)
        self.stream = None

        self.resetChatViewToIdleStatus()

    def _NH_MediaStreamDidFail(self, sender, data):
        self.mediastream_failed = True # used to avoid double printing chat disconnect message
        self.sessionController.log_info(u"Chat stream failed: %s" % data.reason)
        if data.reason in ('Connection was closed cleanly.', 'A TLS packet with unexpected length was received.', 'Cannot send chunk because MSRPSession is DONE'):
            self.chatViewController.showSystemMessage('Connection has been closed', datetime.datetime.now(tzlocal()), True)
        else:
            reason = 'Timeout' if data.reason == 'MSRPConnectTimeout' else data.reason
            self.chatViewController.showSystemMessage('Connection failed: %s' % reason, datetime.datetime.now(tzlocal()), True)

        self.changeStatus(STREAM_FAILED, data.reason)

    def resetIsComposingTimer(self, refresh):
        if self.remoteTypingTimer:
            # if we don't get any indications in the request refresh, then we assume remote to be idle
            self.remoteTypingTimer.setFireDate_(NSDate.dateWithTimeIntervalSinceNow_(refresh))
        else:
            self.remoteTypingTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(refresh, self, "remoteBecameIdle:", None, False)

    def resetFancyTabIcons(self):
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if window:
            window.noteSession_isComposing_(self.sessionController, False)
            window.noteSession_isScreenSharing_(self.sessionController, False)

    def disconnectChatViewHandler(self):
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if window:
            self.handler.setDisconnected()
            window.noteSession_isComposing_(self.sessionController, False)
        else:
            self.handler = None

    def disconnectScreensharingHandler(self):
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        if window:
            self.screensharing_handler.setDisconnected()
        else:
            self.screensharing_handler = None

        self.share_screen_in_conference = False

    def resetChatViewToIdleStatus(self):
        # save the view so we can print it when chat is idle
        self.sessionController.lastChatOutputView = self.chatViewController.outputView

        self.removeFromSession()
        self.videoContainer.hideVideo()
        self.exitFullScreen()
        self.setScreenSharingToolbarIcon()
        self.resetEditorToolbarIcon()
        self.resetFancyTabIcons()
        self.disconnectChatViewHandler()
        self.disconnectScreensharingHandler()

    def endStream(self):
        if self.status != STREAM_DISCONNECTING:
            self.backend.ringer.stop_ringing(self.sessionController.session)

        if self.status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self.stream)
            self.changeStatus(STREAM_CANCELLING)
        elif self.session and self.stream and (self.session.streams == [self.stream] or self.session.remote_focus):
            self.sessionController.end()
            self.changeStatus(STREAM_DISCONNECTING)
        else:
            self.sessionController.endStream(self)
            self.changeStatus(STREAM_DISCONNECTING)

    def closeTab(self):
        # executed when user clicks close tab button or indirectly when user closes the whole window
        self.endStream()

        self.resetChatViewToIdleStatus()

        # remove held reference needed for printing
        self.sessionController.lastChatOutputView = None

        # remove held reference needed by the GUI
        window = ChatWindowManager.ChatWindowManager().getChatWindow(self.sessionController)
        try:
            window.chat_controllers.remove(self)
        except KeyError:
            pass

        # remove allocated tab/window
        ChatWindowManager.ChatWindowManager().removeChatWindow(self.sessionController)

