# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *
from Quartz import *

import datetime
import hashlib
import os
import time
import unicodedata
import uuid

from application.notification import IObserver, NotificationCenter, NotificationData
from application.system import makedirs
from application.python import Null
from itertools import chain
from zope.interface import implements

from resources import ApplicationData
from sipsimple.account import BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams import ChatStream, ChatStreamError
from sipsimple.streams.applications.chat import CPIMIdentity
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp

from util import *

import ChatWindowController

from BlinkLogger import BlinkLogger
from ChatViewController import *
from ContactListModel import encode_icon, decode_icon

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

    fullScreenVideoPanel = objc.IBOutlet()
    fullScreenVideoPanelToobar = objc.IBOutlet()

    document = None
    fail_reason = None
    sessionController = None
    stream = None
    finishedLoading = False
    showHistoryEntries = 20
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

    nickname_request_map = {} # message id -> nickname

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

        self.history_msgid_list=set()

        self.remote_uri = format_identity_to_string(self.sessionController.remotePartyObject)
        self.local_uri = '%s@%s' % (self.sessionController.account.id.username, self.sessionController.account.id.domain) if self.sessionController.account is not BonjourAccount() else 'bonjour'

        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, name='BlinkFileTransferDidEnd')
        self.notification_center.add_observer(self, name='BlinkMuteChangedState')
        self.notification_center.add_observer(self, name='ChatReplicationJournalEntryReceived')

        NSBundle.loadNibNamed_owner_("ChatView", self)

        self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("ChatView", "html"))

        self.chatViewController.setAccount_(self.sessionController.account)
        self.chatViewController.resetRenderedMessages()

        self.outgoing_message_handler = OutgoingMessageHandler.alloc().initWithView_(self.chatViewController)

        self.screensharing_handler = ConferenceScreenSharingHandler()
        self.screensharing_handler.setDelegate(self)

        self.history = ChatHistory()
        self.backend = SIPManager()

        return self

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
            self.chatViewController.showSystemMessage(message, timestamp, is_error)

    def insertSmiley_(self, sender):
        smiley = sender.representedObject()
        self.chatViewController.appendAttributedString_(smiley)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def changeStatus(self, newstate, fail_reason=None):
        self.status = newstate
        MediaStream.changeStatus(self, newstate, fail_reason)
        if self.status == STREAM_FAILED:
            self.reset()

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
        ws = NSWorkspace.sharedWorkspace()
        filenames = [unicodedata.normalize('NFC', file) for file in fnames if os.path.isfile(file)]
        if filenames:
            self.sessionControllersManager.send_files_to_contact(self.sessionController.account, self.sessionController.target_uri, filenames)
            return True
        return False

    def sendOwnIcon(self):
        if self.stream and not self.sessionController.session.remote_focus:
            base64icon = encode_icon(self.chatWindowController.own_icon)
            self.stream.send_message(str(base64icon), content_type='application/blink-icon', timestamp=ISOTimestamp.now())

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

            self.video_frame_visible = False

    @run_in_green_thread
    @allocate_autorelease_pool
    def replay_history(self):
        if not self:
            return
        if self.sessionController.account is not BonjourAccount():
            results = self.history.get_messages(local_uri=self.local_uri, remote_uri=self.remote_uri, media_type='chat', count=self.showHistoryEntries)

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
            self.render_history_messages(messages_to_render)

        self.send_pending_message()

    @allocate_autorelease_pool
    @run_in_gui_thread
    def render_history_messages(self, messages):
        for message in messages:
            if message.direction == 'outgoing':
                icon = NSApp.delegate().contactsWindowController.iconPathForSelf()
            else:
                sender_uri = sipuri_components_from_string(message.cpim_from)[0]
                icon = NSApp.delegate().contactsWindowController.iconPathForURI(sender_uri)

            timestamp=ISOTimestamp(message.cpim_timestamp)
            is_html = message.content_type != 'text'
            private = bool(int(message.private))

            if self.chatViewController:
                self.chatViewController.showMessage(message.msgid, message.direction, message.cpim_from, icon, message.body, timestamp, is_private=private, recipient=message.cpim_to, state=message.status, is_html=is_html, history_entry=True)

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

    def updateToolbarButtons(self, toolbar, got_proposal=False):
        """Called by ChatWindowController when receiving various middleware notifications"""
        settings = SIPSimpleSettings()

        if self.sessionController.hasStreamOfType("audio"):
            audio_stream = self.sessionController.streamHandlerOfType("audio")

        for item in toolbar.visibleItems():
            identifier = item.itemIdentifier()
            if identifier == 'connect_button':
                if self.status in (STREAM_CONNECTING, STREAM_WAITING_DNS_LOOKUP):
                    item.setEnabled_(True)
                    item.setToolTip_('Click to cancel the chat session')
                    item.setLabel_(u'Cancel')
                    item.setImage_(NSImage.imageNamed_("stop_chat"))
                elif self.status == STREAM_PROPOSING:
                    if self.sessionController.proposalOriginator == 'remote':
                        item.setEnabled_(False)
                    else:
                        item.setToolTip_('Click to cancel the chat session')
                        item.setLabel_(u'Cancel')
                        item.setImage_(NSImage.imageNamed_("stop_chat"))
                        item.setEnabled_(True)
                elif self.status == STREAM_CONNECTED:
                    item.setEnabled_(True)
                    item.setToolTip_('Click to stop the chat session')
                    item.setLabel_(u'Disconnect')
                    item.setImage_(NSImage.imageNamed_("stop_chat"))
                else:
                    item.setEnabled_(True)
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
                item.setImage_(NSImage.imageNamed_("recording1" if self.sessionController.hasStreamOfType("audio") and audio_stream.status == STREAM_CONNECTED and audio_stream.stream.recording_active else "record"))
            elif identifier == 'video' and self.sessionControllersManager.isMediaTypeSupported('video'):
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
                item.setImage_(NSImage.imageNamed_("editor-changed" if not self.chatViewController.editorStatus and self.chatViewController.editor_has_changed else "editor"))
            elif identifier == 'screenshot':
                item.setEnabled_(True if self.status == STREAM_CONNECTED and self.sessionControllersManager.isMediaTypeSupported('file-transfer') else False)
            elif identifier == 'sendfile':
                item.setEnabled_(True if self.status == STREAM_CONNECTED and self.sessionControllersManager.isMediaTypeSupported('file-transfer') else False)

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
                if self.status in (STREAM_CONNECTING, STREAM_WAITING_DNS_LOOKUP):
                    return True
                elif self.status in (STREAM_PROPOSING, STREAM_CONNECTED):
                    return True if self.sessionController.canCancelProposal() else False
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

            if identifier == 'audio' and self.status == STREAM_CONNECTED:
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_PROPOSING or audio_stream.status == STREAM_RINGING:
                        self.sessionController.cancelProposal(audio_stream)
                    else:
                        self.sessionController.removeAudioFromSession()

                    sender.setToolTip_('Click to add audio to this session')
                    sender.setImage_(NSImage.imageNamed_("audio"))
                    self.chatWindowController.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                    self.chatWindowController.audioStatus.setStringValue_(u"Connected")

                    # The button will be enabled again after operation is finished
                    sender.setEnabled_(False)
                else:
                    self.sessionController.addAudioToSession()
                    sender.setToolTip_('Click to cancel the audio call')
                    sender.setImage_(NSImage.imageNamed_("hangup"))
                    self.notification_center.post_notification("SIPSessionGotRingIndication", sender=self.sessionController.session)
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

            elif identifier == 'smileys':
                self.chatViewController.expandSmileys = not self.chatViewController.expandSmileys
                sender.setImage_(NSImage.imageNamed_("smiley_on" if self.chatViewController.expandSmileys else "smiley_off"))
                self.chatViewController.toggleSmileys(self.chatViewController.expandSmileys)

            elif identifier == 'editor' and self.sessionController.account is not BonjourAccount() and not settings.chat.disable_collaboration_editor:
                sender.setImage_(NSImage.imageNamed_("editor"))
                sender.setToolTip_("Switch to Chat Session" if self.chatViewController.editorStatus else "Enable Collaborative Editor")
                self.toggleEditor()
            elif identifier == 'history' and NSApp.delegate().applicationName != 'Blink Lite':
                contactWindow = NSApp.delegate().contactsWindowController
                contactWindow.showHistoryViewer_(None)
                if self.sessionController.account is BonjourAccount():
                    contactWindow.historyViewer.filterByURIs(('bonjour', ), media_type='chat')
                else:
                    contactWindow.historyViewer.filterByURIs((format_identity_to_string(self.sessionController.target_uri),), media_type='chat')

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
        self.chatViewController.editor_has_changed = False
        self.chatViewController.editorStatus = not self.chatViewController.editorStatus
        self.showChatViewWithEditorWhileVideoActive()
        self.chatViewController.toggleCollaborationEditor(self.chatViewController.editorStatus)
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
        if message.content_type == 'application/blink-icon':
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
            timestamp = message.timestamp
            is_html = True if message.content_type == 'text/html' else False
            name = format_identity_to_string(sender, format='compact')
            icon = NSApp.delegate().contactsWindowController.iconPathForURI(format_identity_to_string(sender), self.session.remote_focus)
            recipient_html = '%s <%s@%s>' % (recipient.display_name, recipient.uri.user, recipient.uri.host) if recipient else ''
            if self.chatViewController:
                self.chatViewController.showMessage(msgid, 'incoming', name, icon, text, timestamp, is_private=private, recipient=recipient_html, state="delivered", is_html=is_html)

            tab = self.chatViewController.outputView.window()
            tab_is_key = tab.isKeyWindow() if tab else False
            tab = None

            # FancyTabViewSwitcher will set unfocused tab item views as Hidden
            if not tab_is_key or self.chatViewController.view.isHiddenOrHasHiddenAncestor():
                # notify growl
                growl_data = NotificationData()
                growl_data.sender = format_identity_to_string(sender, format='compact')
                growl_data.content = html2txt(message.body[0:400]) if message.content_type == 'text/html' else message.body[0:400]
                NotificationCenter().post_notification("GrowlGotChatMessage", sender=self, data=growl_data)
                NSApp.requestUserAttention_(NSInformationalRequest)

            NotificationCenter().post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=NotificationData(direction='incoming', history_entry=False, remote_party=format_identity_to_string(self.sessionController.remotePartyObject, format='full'), local_party=format_identity_to_string(self.sessionController.account) if self.sessionController.account is not BonjourAccount() else 'bonjour', check_contact=True))

            # save to history
            message = MessageInfo(msgid, direction='incoming', sender=sender, recipient=recipient, timestamp=timestamp, text=text, private=private, status="delivered", content_type='html' if is_html else 'text')
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
                self.chatViewController.showMessage(str(uuid.uuid1()), 'incoming', name, icon, text, timestamp, state="delivered", history_entry=True, is_html=True)

    def _NH_BlinkSessionDidFail(self, sender, data):
        reason = data.failure_reason or data.reason
        if reason != 'Session Cancelled':
            if self.last_failure_reason != reason:
                self.last_failure_reason = reason
        if not self.mediastream_failed and not self.mediastream_ended:
            if reason != 'Session Cancelled':
                message = "Cannot establish connection: %s" % reason
            else:
                message = "Session Cancelled"
            self.showSystemMessage(message, ISOTimestamp.now(), True)
        else:
            self.showSystemMessage(reason, ISOTimestamp.now(), True)
        self.changeStatus(STREAM_FAILED)

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
                message = "Proposal rejected: %s (%s)" % (reason, data.code) if data.code != 200 else "Proposal rejected"
                self.showSystemMessage(message, ISOTimestamp.now(), True)

    def _NH_MediaStreamDidStart(self, sender, data):
        self.mediastream_started = True
        self.last_failure_reason = None
        endpoint = str(self.stream.msrp.full_remote_path[0])
        self.sessionController.log_info(u"Chat stream established to %s" % endpoint)
        self.showSystemMessage("Session established", ISOTimestamp.now())

        # Set nickname if available
        nickname = self.sessionController.nickname
        self.sessionController.nickname = None
        self.setNickname(nickname)

        self.outgoing_message_handler.setConnected(self.stream)

        # needed to set the Audio button state after session has started
        self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self)

        self.changeStatus(STREAM_CONNECTED)
        self.sendOwnIcon()

    def _NH_MediaStreamDidEnd(self, sender, data):
        self.mediastream_ended = True
        self.sessionController.log_info(u"Chat stream ended")
        self.notification_center.remove_observer(self, sender=sender)
        self.notification_center.remove_observer(self, sender=self.sessionController)
        if self.mediastream_started:
            close_message = "%s has left the conversation" % self.sessionController.getTitleShort()
            self.showSystemMessage(close_message, ISOTimestamp.now())
        self.changeStatus(STREAM_IDLE, self.sessionController.endingBy)
        self.reset()

    def _NH_MediaStreamDidFail(self, sender, data):
        self.mediastream_failed = True
        self.sessionController.log_info(u"Chat stream failed: %s" % data.reason)
        if data.reason in ('Connection was closed cleanly.', 'Cannot send chunk because MSRPSession is DONE'):
            reason = 'Connection has been closed'
        elif data.reason == 'A TLS packet with unexpected length was received.':
            reason = 'TLS connection error'
        elif data.reason in ('MSRPTimeout', 'MSRPConnectTimeout', 'MSRPBindSessionTimeout', 'MSRPIncomingConnectTimeout'):
            reason = 'Network connectivity failure'
        elif data.reason == 'MSRPRelayConnectTimeout':
            reason = 'Timeout connecting to MSRP relay'
        elif data.reason == 'MSRPRelayAuthError':
            reason = 'Failed to authenticate to MSRP relay'
        else:
            reason = data.reason

        self.showSystemMessage(reason, ISOTimestamp.now(), True)
        self.changeStatus(STREAM_FAILED, data.reason)

    def _NH_ChatReplicationJournalEntryReceived(self, sender, data):
        if self.status == STREAM_CONNECTED:
            return

        data = data.chat_message
        if self.local_uri != data['local_uri'] or self.remote_uri != data['remote_uri']:
            return

        icon = NSApp.delegate().contactsWindowController.iconPathForURI(data['cpim_to'])
        timestamp = ISOTimestamp(data['cpim_timestamp'])
        self.chatViewController.showMessage(data['msgid'], data['direction'], data['cpim_from'], icon, data['body'], timestamp, is_private=bool(int(data['private'])), recipient=data['cpim_to'], state=data['status'], is_html=True, history_entry=True)

    def resetIsComposingTimer(self, refresh):
        if self.remoteTypingTimer:
            # if we don't get any indications in the request refresh, then we assume remote to be idle
            self.remoteTypingTimer.setFireDate_(NSDate.dateWithTimeIntervalSinceNow_(refresh))
        else:
            self.remoteTypingTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(refresh, self, "remoteBecameIdle:", None, False)

    def endStream(self):
        streamController = self.sessionController.streamHandlerOfType('chat')

        if self.status != STREAM_DISCONNECTING:
            self.sessionControllersManager.ringer.stop_ringing(self.sessionController.session)

        if self.status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self.stream)
            self.changeStatus(STREAM_CANCELLING)
        elif self.session and self.stream and (self.sessionController.streamHandlers == [self] or self.session.remote_focus):
            self.sessionController.end()
            self.changeStatus(STREAM_DISCONNECTING)
        else:
            #self.sessionController.end()
            self.sessionController.endStream(self)
            self.changeStatus(STREAM_DISCONNECTING)

    # lifetime of a chat controler: possible deallocation paths
    # 1. User click on close tab: closeTab -> endStream -> reset -> CloseWindow -> deallocTimer -> dealloc
    # 2. User clicks on close window: closeWindow -> for each tab -> closeTab -> endStream -> reset -> CloseWindow -> deallocTimer -> dealloc
    # 3. Session ends by remote: mediaDidEnd -> endStream -> reset -> CloseWindow -> deallocTimer -> dealloc
    # 4. User clicks on disconnect button: endStream -> reset

    def closeTab(self):
        self.endStream()
        self.reset()
        self.closeWindow()
        self.startDeallocTimer()

    def reset(self):
        self.mediastream_failed = False
        self.mediastream_ended = False
        self.session_succeeded = False
        self.mediastream_started = False
        self.last_failure_reason = None
        self.remoteIcon = None
        self.share_screen_in_conference = False

        self.videoContainer.hideVideo()
        self.exitFullScreen()
        self.setScreenSharingToolbarIcon()
        self.resetEditorToolbarIcon()

        # save chat view so we can print it when session is over
        self.sessionController.chatPrintView = self.chatViewController.outputView

        self.chatWindowController.noteSession_isComposing_(self.sessionController, False)
        self.chatWindowController.noteSession_isScreenSharing_(self.sessionController, False)

        if self.outgoing_message_handler:
            self.outgoing_message_handler.setDisconnected()

        if self.screensharing_handler:
            self.screensharing_handler.setDisconnected()

    def startDeallocTimer(self):
        self.removeFromSession()

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
        self.notification_center = None

        # remove GUI observers
        NSNotificationCenter.defaultCenter().removeObserver_(self)

        # dealloc timers
        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
            self.remoteTypingTimer = None

        self.dealloc_timer.invalidate()
        self.dealloc_timer = None

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

        super(ChatController, self).dealloc()


class MessageInfo(object):
    def __init__(self, msgid, direction='outgoing', sender=None, recipient=None, timestamp=None, text=None, private=False, status=None, content_type='text', pending=False):
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

    def _send(self, msgid):
        message = self.messages.pop(msgid)
        if message.private and message.recipient is not None:
            try:
                id = self.stream.send_message(message.text, timestamp=message.timestamp, recipients=[message.recipient])
                self.no_report_received_messages[msgid] = message
            except ChatStreamError, e:
                BlinkLogger().log_error(u"Error sending message: %s" % e)
                self.delegate.markMessage(msgid, MSG_STATE_FAILED, private)
                message.status='failed'
                self.add_to_history(message)
                return False
        else:
            try:
                id = self.stream.send_message(message.text, timestamp=message.timestamp)
                self.no_report_received_messages[msgid] = message
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
                    self._send(msgid)
                except Exception, e:
                    BlinkLogger().log_error(u"Error sending message: %s" % e)
                    self.delegate.showSystemMessage("Error sending message", timestamp, True)
                else:
                    self.delegate.showMessage(msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="sent", recipient=recipient_html)
            else:
                self.messages[msgid].pending=True
                self.delegate.showMessage(msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="queued", recipient=recipient_html)

        return True

    def resend(self, msgid, text, recipient=None, private=False):
        timestamp = ISOTimestamp.now()
        recipient_html = "%s <%s@%s>" % (recipient.display_name, recipient.uri.user, recipient.uri.host) if recipient else ''
        icon = NSApp.delegate().contactsWindowController.iconPathForSelf()

        self.messages[msgid] = MessageInfo(msgid=msgid, recipient=recipient, timestamp=timestamp, text=text, private=private, status="queued")

        if self.connected:
            try:
                self._send(msgid)
            except Exception, e:
                BlinkLogger().log_error(u"Error sending message: %s" % e)
                self.delegate.showSystemMessage("Error sending message", timestamp, True)
            else:
                self.delegate.showMessage(msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="sent", recipient=recipient_html)
        else:
            self.messages[msgid].pending=True
            self.delegate.showMessage(msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="queued", recipient=recipient_html)

    def setConnected(self, stream):
        self.no_report_received_messages = {}
        self.connected = True
        self.stream = stream
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
                    BlinkLogger().log_error(u"Error sending queued message: %s" % msgid)
                else:
                    self.delegate.markMessage(msgid, MSG_STATE_SENDING, private)

    def setDisconnected(self):
        self.connected = False
        self.connected = False
        pending = (msgid for msgid in self.messages.keys() if self.messages[msgid].pending)
        for msgid in pending:
            try:
                message = self.messages.pop(msgid)
            except KeyError:
                pass
            else:
                message.status = 'failed'
                self.delegate.markMessage(msgid, MSG_STATE_FAILED)
                self.add_to_history(message)

        self.messages = {}
        for msgid in self.no_report_received_messages.keys():
            try:
                message = self.no_report_received_messages.pop(msgid)
            except KeyError:
                pass
            else:
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
                try:
                    del self.no_report_received_messages[message.msgid]
                except KeyError:
                    pass
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
                try:
                    del self.no_report_received_messages[message.msgid]
                except KeyError:
                    pass
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
        cpim_from = format_identity_to_string(message.sender, format='full') if message.sender else ''
        cpim_timestamp = str(message.timestamp)
        private = "1" if message.private else "0"
        self.history.add_message(message.msgid, 'chat', self.local_uri, self.remote_uri, message.direction, cpim_from, cpim_to, cpim_timestamp, message.text, message.content_type, private, message.status)


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
                BlinkLogger().log_info('Sending %s bytes %s screen width' % (format_size(len(jpeg)), image.size().width))
                self.log_first_frame = False
            BlinkLogger().log_debug('Sending %s bytes %s screen width ' % (format_size(len(jpeg)), image.size().width))
            self.may_send = False
            if self.stream:
                self.stream.send_message(str(jpeg), content_type='application/blink-screensharing', timestamp=ISOTimestamp.now())

