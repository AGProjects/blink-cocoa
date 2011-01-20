# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *

import datetime
import hashlib
import os
import time

from itertools import dropwhile, takewhile

from application import log
from application.notification import IObserver, NotificationCenter
from application.python.util import Null
from zope.interface import implements

from sipsimple.account import Account, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams import ChatStream, ChatStreamError
from sipsimple.streams.applications.chat import CPIMIdentity
from sipsimple.util import TimestampedNotificationData, Timestamp

import SessionController
import ChatWindowManager

from FileTransferWindowController import openFileTransferSelectionDialog
from MediaStream import *
from SessionHistory import SessionHistory
from BlinkLogger import BlinkLogger
from ChatViewController import *
from SIPManager import SIPManager
from SmileyManager import SmileyManager
from util import *


DELIVERY_TIMEOUT = 5.0
MAX_MESSAGE_LENGTH = 16*1024

def userClickedToolbarButtonWhileDisconnected(sessionController, sender):
    """
    Called by ChatWindowController when dispatching toolbar button clicks to the selected Session tab.
    """
    tag = sender.tag()
    if tag == SessionController.TOOLBAR_RECONNECT:
        BlinkLogger().log_info("Re-establishing session to %s" % sessionController.remoteParty)
        sessionController.startChatSession()
    elif tag == SessionController.TOOLBAR_HISTORY:
        contactWindow = sessionController.owner
        contactWindow.showChatTranscripts_(None)
        if sessionController.account is BonjourAccount():
            contactWindow.transcriptViewer.filterByContactAccount('bonjour', sessionController.account)
        else:
            contactWindow.transcriptViewer.filterByContactAccount(format_identity(sessionController.target_uri), sessionController.account)


def validateToolbarButtonWhileDisconnected(sessionController, item):
    return item.tag() in (SessionController.TOOLBAR_RECONNECT, SessionController.TOOLBAR_HISTORY)

def updateToolbarButtonsWhileDisconnected(sessionController, toolbar):
    for item in toolbar.visibleItems():
        tag = item.tag()
        if tag == SessionController.TOOLBAR_RECONNECT:
            item.setEnabled_(True)
        elif tag == SessionController.TOOLBAR_AUDIO:
            item.setToolTip_('Click to add audio to this session')
            item.setImage_(NSImage.imageNamed_("audio"))
            item.setEnabled_(False)
        elif tag == SessionController.TOOLBAR_RECORD:
            item.setImage_(NSImage.imageNamed_("record"))
            item.setEnabled_(False)
        elif tag == SessionController.TOOLBAR_HOLD:
            item.setImage_(NSImage.imageNamed_("pause"))
            item.setEnabled_(False)
        elif tag == SessionController.TOOLBAR_VIDEO:
            item.setImage_(NSImage.imageNamed_("video"))
            item.setEnabled_(False)
        elif tag == SessionController.TOOLBAR_SEND_FILE:
            item.setEnabled_(False)
        elif tag == SessionController.TOOLBAR_DESKTOP_SHARING_BUTTON:
            item.setEnabled_(False)
        elif tag == SessionController.TOOLBAR_SMILEY:
            item.setImage_(NSImage.imageNamed_("smiley_on"))
            item.setEnabled_(False)


class MessageInfo(object):
    def __init__(self, id, msgid, timestamp, state, private):
        self.id = id
        self.msgid = msgid
        self.timestamp = timestamp
        self.state = state
        self.private = private


class MessageHandler(NSObject):
    """
    Until the stream is connected, all messages typed will be queued and
    marked internally as queued.  Once the stream is connected, queued
    messages will be sent.

    Sent messages are internally marked as  unconfirmed. In the UI they are
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

    messages = None
    pending = None
    session = None
    stream = None
    connected = False

    lastDeliveredTime = None

    delegate = None

    def initWithSession_(self, session):
        self = super(MessageHandler, self).init()
        if self:
            self.session = session
            self.stream = None
            self.connected = None
            self.messages = {}
            self.pending = []

        return self

    def setDelegate(self, delegate):
        self.delegate = delegate

    def _send(self, msgid, text, timestamp, recipient=None, private=False):
        if private and recipient is not None:
            try:
                id = self.stream.send_message(text, timestamp=timestamp, recipients=[recipient])
            except ChatStreamError, e:
                BlinkLogger().log_error("Error sending message: %s" % e)
                self.delegate.markMessage(msgid, MSG_STATE_FAILED, private)
                return False
        else:
            try:
                id = self.stream.send_message(text, timestamp=timestamp)
            except ChatStreamError, e:
                BlinkLogger().log_error("Error sending message: %s" % e)
                self.delegate.markMessage(msgid, MSG_STATE_FAILED, private)
                return False

        message = MessageInfo(id, msgid, timestamp, MSG_STATE_SENDING, private)
        self.messages[id] = message
        return message

        return True

    def send(self, text, recipient=None, private=False):
        now = datetime.datetime.utcnow()
        timestamp = Timestamp(now)
        icon = NSApp.delegate().windowController.iconPathForSelf()
        recipient_html = "%s <%s@%s>" % (recipient.display_name, recipient.uri.user, recipient.uri.host) if recipient else ''
        leftover = text
        while leftover:
            # if the text is too big, break it in a smaller size.. without corrupting
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

            if self.connected:
                try:
                    self._send(msgid, text, now, recipient, private)
                except Exception, e:
                    log.err()
                    BlinkLogger().log_error("Error sending message: %s" % e)
                    self.delegate.showSystemMessage("Error sending message",now, True)
                else:
                    self.delegate.showMessage(msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="sent", recipient=recipient_html)
            else:
                self.pending.append((msgid, text, now, recipient, private))
                self.delegate.showMessage(msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="queued", recipient=recipient_html)

        return True

    def resend(self, msgid, text, recipient=None, private=False):
        now = datetime.datetime.utcnow()
        timestamp = Timestamp(now)
        recipient_html = "%s <%s@%s>" % (recipient.display_name, recipient.uri.user, recipient.uri.host) if recipient else ''
        icon = NSApp.delegate().windowController.iconPathForSelf()
        if self.connected:
            try:
                self._send(msgid, text, now, recipient, private)
            except Exception, e:
                BlinkLogger().log_error("Error sending message: %s" % e)
                self.delegate.showSystemMessage("Error sending message",now, True)
            else:
                self.delegate.showMessage(msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="sent", history_entry=True, recipient=recipient)
        else:
            self.pending.append((msgid, text, now, recipient, private))
            self.delegate.showMessage(msgid, 'outgoing', None, icon, text, timestamp, is_private=private, state="queued", history_entry=True, recipient=recipient)

    def setConnected(self, stream):
        self.stream = stream
        NotificationCenter().add_observer(self, sender=stream)
        icon = NSApp.delegate().windowController.iconPathForSelf()
        for msgid, text, timestamp, recipient, private in self.pending:
            try:
                message = self._send(msgid, text, timestamp, recipient, private)
            except Exception, e:
                log.err()
                BlinkLogger().log_error("Error sending queued message: %s" % e)
            else:
                self.delegate.markMessage(msgid, MSG_STATE_SENDING, private)

        self.pending = []

    def setDisconnected(self):
        self.connected = False
        for msgid, text, timestamp, recipient, private in self.pending:
            self.delegate.markMessage(msgid, MSG_STATE_FAILED)
        if self.stream:
            NotificationCenter().remove_observer(self, sender=self.stream)
            self.stream = None

    def shakyConnectionMode(self):
        return self.messages and time.time() - self.lastDeliveredTime >= DELIVERY_TIMEOUT or any(m for m in self.messages.itervalues() if m.state==MSG_STATE_FAILED)

    def markMessage(self, message, state):
        message.state = state
        self.delegate.markMessage(message.msgid, state, message.private)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_ChatStreamDidDeliverMessage(self, sender, data):
        message = self.messages.pop(data.message_id)
        self.markMessage(message, MSG_STATE_DELIVERED)
        self.lastDeliveredTime = time.time()

    def _NH_ChatStreamDidNotDeliverMessage(self, sender, data):
        message = self.messages.pop(data.message_id)
        self.markMessage(message, MSG_STATE_FAILED)


class ChatController(MediaStream):
    implements(IObserver)

    chatViewController = objc.IBOutlet()
    smileyButton = objc.IBOutlet()
    upperContainer = objc.IBOutlet()
    addContactView = objc.IBOutlet()
    addContactLabel = objc.IBOutlet()

    document = None
    fail_reason = None
    sessionController = None
    stream = None
    finishedLoading = False
    sysMessageQueue = []
    sentMessagesPendingConfirmation = []
    showHistoryEntries = 50

    loggingEnabled = True
    history = None

    handler = None
    wasRemoved = False

    lastDeliveredTime = None
    undeliveredMessages = {} # id -> message

    # timer is reset whenever remote end sends is-composing active, when it times out, go to idle
    remoteTypingTimer = None

    @classmethod
    def createStream(self, account):
        return ChatStream(account)


    def initWithOwner_stream_(self, scontroller, stream):
        self = super(ChatController, self).initWithOwner_stream_(scontroller, stream)

        if self:
            self.last_history_entry = None
            self.history_msgid_list=set()

            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, sender=stream)
            self.notification_center.add_observer(self, sender=self.sessionController.session)

            NSBundle.loadNibNamed_owner_("ChatView", self)

            self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("ChatView", "html"))
            self.chatViewController.setAccount_(self.sessionController.account)
            self.chatViewController.resetRenderedMessages()

            self.handler = MessageHandler.alloc().initWithSession_(self.sessionController.session)
            self.handler.setDelegate(self.chatViewController)

            if self.loggingEnabled:
                try:
                    uri = format_identity_address(self.sessionController.remotePartyObject)
                    contact = NSApp.delegate().windowController.getContactMatchingURI(uri)
                    if contact:
                        uri = str(contact.uri)

                    self.history = SessionHistory().open_chat_history(self.sessionController.account, uri)
                    self.chatViewController.setHistory_(self.history)
                    
                except Exception, exc:
                    self.loggingEnabled = False
                    self.chatViewController.showSystemMessage("Unable to create Chat History file: %s"%exc, datetime.datetime.utcnow(), True)

                if self.showHistoryEntries > 0:
                    if self.sessionController.account is BonjourAccount():
                        self.history_entries = SessionHistory().get_chat_history(self.sessionController.account, 'bonjour', self.showHistoryEntries)
                    else:
                        self.history_entries = SessionHistory().get_chat_history(self.sessionController.account, uri, self.showHistoryEntries)

                    for entry in self.history_entries:
                        self.history_msgid_list.add(entry["id"])

                    try:
                        self.last_history_entry = self.history_entries[-1]['send_time']
                    except (KeyError, IndexError):
                        pass

            # Chat drawer has now contextual menu for adding contacts
            #if isinstance(self.sessionController.account, Account) and self.sessionController.session.direction == 'incoming' and not NSApp.delegate().windowController.hasContactMatchingURI(scontroller.target_uri):
            #    self.enableAddContactPanel()

        return self

    def awakeFromNib(self):
        #self.chatViewController.inputText.setMaxLength_(MAX_MESSAGE_LENGTH)

        # setup smiley popup 
        smileys = SmileyManager().get_smiley_list()
        menu = self.smileyButton.menu()
        while menu.numberOfItems() > 0:
            menu.removeItemAtIndex_(0)
        bigText = NSAttributedString.alloc().initWithString_attributes_(" ", NSDictionary.dictionaryWithObject_forKey_(NSFont.systemFontOfSize_(16), NSFontAttributeName))
        for text, file in smileys:
            image = NSImage.alloc().initWithContentsOfFile_(file)
            if not image:
                print "cant load %s"%file
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

    def dealloc(self):
        if self.history:
            self.history.close()
            self.history = None
          
        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
        
        super(ChatController, self).dealloc()

    def getContentView(self):
        return self.chatViewController.view

    def insertSmiley_(self, sender):
        smiley = sender.representedObject()
        self.chatViewController.appendAttributedString_(smiley)

    def end(self, autoclose=False):
        log_info(self, "Ending session in %s"%self.status)
        status = self.status
        if status in (STREAM_IDLE, STREAM_FAILED):
            self.closeChatWindow()
        elif status != STREAM_DISCONNECTING:
            self.changeStatus(STREAM_DISCONNECTING)
            if status == STREAM_PROPOSING or status == STREAM_RINGING:
                self.sessionController.cancelProposal(self.stream)
                self.changeStatus(STREAM_CANCELLING)
            elif self.stream and self.session.streams and len(self.session.streams) > 0:
                log_info(self, "Removing Chat Stream from session")                    
                self.sessionController.endStream(self)
            else:
                self.sessionController.end()
        if autoclose or status == STREAM_DISCONNECTING:
            self.closeChatWindow()

    @allocate_autorelease_pool
    @run_in_gui_thread
    def changeStatus(self, newstate, fail_reason=None):
        ended = False
        log_debug(self, "Changing chat state to "+newstate)
        if newstate == STREAM_CONNECTED:
            endpoint = str(self.stream.msrp.full_remote_path[0])
            BlinkLogger().log_info("Session established to %s (%s)"%(endpoint, self.remoteParty))
            self.chatViewController.showSystemMessage("Session established", datetime.datetime.utcnow())
        elif newstate == STREAM_DISCONNECTING:
            BlinkLogger().log_info("Ending session")
        elif newstate == STREAM_CANCELLING:
            BlinkLogger().log_info("Cancelling Chat Proposal")
        elif newstate == STREAM_IDLE:
            if self.status not in (STREAM_FAILED, STREAM_IDLE):
                BlinkLogger().log_info("Chat session ended (%s)"%fail_reason)
                if self.status == STREAM_CONNECTED:
                    close_message = "%s has left the conversation" % self.sessionController.getTitleShort()
                    self.chatViewController.showSystemMessage(close_message, datetime.datetime.utcnow())
                ended = True
        elif newstate == STREAM_FAILED:
            if self.status not in (STREAM_FAILED, STREAM_IDLE):
                if fail_reason:
                    BlinkLogger().log_error("Chat session failed: %s" % fail_reason)
                    self.chatViewController.showSystemMessage("Session failed: %s" % fail_reason, datetime.datetime.utcnow(), True)
                else:
                    BlinkLogger().log_error("Chat session failed")
                    self.chatViewController.showSystemMessage("Session failed", datetime.datetime.utcnow(), True)
                ended = True
        self.status = newstate
        MediaStream.changeStatus(self, newstate, fail_reason)
        if ended and self.stream:
            if self.handler:
                self.handler.setDisconnected()
            # don't close history here as it would not allow us to log queued messages anymore
            self.removeFromSession()

        self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self)

    def startOutgoing(self, is_update):
        ChatWindowManager.ChatWindowManager().addChatSession(self.sessionController)
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_WAITING_DNS_LOOKUP)

    def startIncoming(self, is_update):
        ChatWindowManager.ChatWindowManager().addChatSession(self.sessionController)
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_INCOMING)

    def closeChatWindow(self):
        ChatWindowManager.ChatWindowManager().removeChatSession(self.sessionController)

    def sendFiles(self, fnames):
        ws = NSWorkspace.sharedWorkspace()
        names_and_types = []
        for f in fnames:
            ctype, error = ws.typeOfFile_error_(f, None)
            if ctype:
                names_and_types.append((unicode(f), str(ctype)))
            else:
                print "%f : %s"%(f,error)
        if names_and_types:
            try:
                SIPManager().send_files_to_contact(self.sessionController.account, self.sessionController.target_uri, names_and_types)
                return True
            except:
                import traceback
                traceback.print_exc()
        return False

    @objc.IBAction
    def addContactPanelClicked_(self, sender):
        if sender.tag() == 1:
            NSApp.delegate().windowController.addContact(self.sessionController.target_uri)
        self.addContactView.removeFromSuperview()
        frame = self.chatViewController.outputView.frame()
        frame.origin.y = 0
        frame.size = self.upperContainer.frame().size
        self.chatViewController.outputView.setFrame_(frame)
    
    def enableAddContactPanel(self):
        text = u"%s is not in your Contacts List. Would you like to add it now?" % self.sessionController.getTitleShort()
        self.addContactLabel.setStringValue_(text)
        frame = self.chatViewController.outputView.frame()
        frame.size.height -= NSHeight(self.addContactView.frame())
        frame.origin.y += NSHeight(self.addContactView.frame())
        self.chatViewController.outputView.setFrame_(frame)
        self.upperContainer.addSubview_(self.addContactView)
        frame = self.addContactView.frame()
        frame.origin = NSZeroPoint
        self.addContactView.setFrame_(frame)

    def textView_doCommandBySelector_(self, textView, selector):
        if selector == "insertNewline:" and self.chatViewController.inputText == textView:
            original = textView.string()
            text = unicode(original)
            textView.setString_("")
            if text:
                recipient = '%s <sip:%s@%s>' % (self.sessionController.contactDisplayName, self.sessionController.remotePartyObject.user, self.sessionController.remotePartyObject.host)
                try:
                    identity = CPIMIdentity.parse(recipient)
                except ValueError:
                    identity = None
                if not self.handler.send(text, recipient=identity):
                    textView.setString_(original)
            if not self.stream or self.status in [STREAM_FAILED, STREAM_IDLE]:
                if self.history:
                    self.history.close()
                    self.history = None
                BlinkLogger().log_info("Session not established, starting it")
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
        if self.showHistoryEntries > 0:
    
            failed_entries = list(takewhile(lambda entry: entry['state']=='failed', reversed(self.history_entries)))
            old_entries = list(dropwhile(lambda entry: entry['state']=='failed', reversed(self.history_entries)))
            
            failed_entries.reverse()
            old_entries.reverse()
            
            for entry in old_entries:
                msgid = entry["id"]
                stamp = entry["send_time"]
                sender = entry["sender"]
                direction = entry["direction"]
                text = entry["text"]
                is_html = entry["type"] == "html"
                recipient = entry["recipient"]
                state = entry["state"]
                private = entry["private"]
                sender_uri = format_identity_from_text(sender)[0]
                
                if direction == 'outgoing':
                    icon = NSApp.delegate().windowController.iconPathForSelf()
                else:
                    icon = NSApp.delegate().windowController.iconPathForURI(sender_uri)

                try:
                    timestamp=Timestamp.parse(stamp)
                except (TypeError, ValueError):
                    continue

                private = True if private == "1" else False
                chatView.showMessage(msgid, direction, sender, icon, text, timestamp, is_private=private, recipient=recipient, state=state, is_html=is_html, history_entry=True)

        else:
            failed_entries = []

        if self.sessionController.account is not BonjourAccount():
            # do not resend bonjour messages as they might arive at the wrong recipient who broadcasted the same address
            if self.history:
                self.history.pending += failed_entries

            for entry in self.history.pending:
                if entry["recipient"]:
                    address, display_name, full_uri, fancy_uri = format_identity_from_text(entry["recipient"])
                    try:
                        recipient = CPIMIdentity.parse('%s <sip:%s>' % (display_name, address))
                    except ValueError:
                        continue
                else:
                    recipient = None

                private = True if private == "1" else False
                self.handler.resend(entry["id"], entry["text"], recipient, private)

    def chatViewDidGetNewMessage_(self, chatView):
        NSApp.delegate().noteNewMessage(self.chatViewController.outputView.window())
        window = ChatWindowManager.ChatWindowManager().windowForChatSession(self.sessionController)
        if window:
            window.noteNewMessageForSession_(self.sessionController)

    def updateToolbarButtons(self, toolbar, got_proposal=False):
        if self.sessionController.hasStreamOfType("audio"):
            audio_stream = self.sessionController.streamHandlerOfType("audio")

        if self.sessionController.hasStreamOfType("video"):
            video_stream = self.sessionController.streamHandlerOfType("video")

        for item in toolbar.visibleItems():
            tag = item.tag()
            if tag == SessionController.TOOLBAR_RECONNECT:
                if self.status in (STREAM_IDLE, STREAM_FAILED):
                    item.setEnabled_(True)
                else:
                    item.setEnabled_(False)
            elif tag == SessionController.TOOLBAR_AUDIO:
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_PROPOSING or audio_stream.status == STREAM_RINGING:
                        item.setToolTip_('Click to cancel the audio call')
                        item.setImage_(NSImage.imageNamed_("hangup"))
                    elif audio_stream.status == STREAM_CONNECTED:
                        item.setToolTip_('Click to hangup the audio call')
                        item.setImage_(NSImage.imageNamed_("hangup"))
                else:
                    item.setToolTip_('Click to add audio to this session')
                    item.setImage_(NSImage.imageNamed_("audio"))

                if self.status == STREAM_INCOMING:
                    item.setEnabled_(False)
                elif self.status in (STREAM_CONNECTED, STREAM_PROPOSING, STREAM_RINGING) and not got_proposal:
                    item.setEnabled_(True)
                else:
                    item.setEnabled_(False)
            elif tag == SessionController.TOOLBAR_RECORD:
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_CONNECTED:
                        if audio_stream.stream.recording_active:
                            item.setImage_(NSImage.imageNamed_("recording1"))
                        else:
                            item.setImage_(NSImage.imageNamed_("record"))
                        item.setEnabled_(True)
                    else:
                        item.setImage_(NSImage.imageNamed_("record"))
            elif tag == SessionController.TOOLBAR_HOLD:
                if self.sessionController.hasStreamOfType("audio"):
                    if audio_stream.status == STREAM_CONNECTED:
                        if audio_stream.holdByRemote:
                            item.setImage_(NSImage.imageNamed_("paused"))
                        elif audio_stream.holdByLocal:
                            item.setImage_(NSImage.imageNamed_("paused"))
                        else:
                            item.setImage_(NSImage.imageNamed_("pause"))
                        item.setEnabled_(True)
                    else:
                        item.setImage_(NSImage.imageNamed_("pause"))
                        item.setEnabled_(False)
                else:
                    item.setImage_(NSImage.imageNamed_("pause"))
                    item.setEnabled_(False)
            elif tag == SessionController.TOOLBAR_VIDEO:
                item.setEnabled_(False)
                continue
                # TODO: enable video -adi
                if self.sessionController.hasStreamOfType("video"):
                    if video_stream.status == STREAM_PROPOSING or video_stream.status == STREAM_RINGING:
                        item.setToolTip_('Click to cancel the video call')
                        item.setImage_(NSImage.imageNamed_("hangup"))
                    elif video_stream.status == STREAM_CONNECTED:
                        item.setToolTip_('Click to hangup the video call')
                        item.setImage_(NSImage.imageNamed_("hangup"))
                else:
                    item.setToolTip_('Click to add video to this session')
                    item.setImage_(NSImage.imageNamed_("video"))

                if self.status == STREAM_INCOMING:
                    item.setEnabled_(False)
                elif self.status in (STREAM_CONNECTED, STREAM_PROPOSING, STREAM_RINGING) and not got_proposal:
                    item.setEnabled_(True)
                else:
                    item.setEnabled_(False)
            elif tag == SessionController.TOOLBAR_SEND_FILE:
                if self.status == STREAM_CONNECTED:
                    item.setEnabled_(True)
                else:
                    item.setEnabled_(False)
            elif tag == SessionController.TOOLBAR_DESKTOP_SHARING_BUTTON:
                if self.status == STREAM_CONNECTED and not got_proposal and not self.sessionController.remote_focus:
                    item.setEnabled_(True)
                else:
                    item.setEnabled_(False)
                title = self.sessionController.getTitleShort()
                menu = toolbar.delegate().desktopShareMenu
                menu.itemWithTag_(SessionController.TOOLBAR_REQUEST_DESKTOP_MENU).setTitle_("Request Desktop from %s" % title)
                menu.itemWithTag_(SessionController.TOOLBAR_SHARE_DESKTOP_MENU).setTitle_("Share My Desktop with %s" % title)
            elif tag == SessionController.TOOLBAR_SMILEY:
                if self.status == STREAM_CONNECTED:
                    item.setEnabled_(True)
                else:
                    item.setEnabled_(False)
                item.setImage_(NSImage.imageNamed_("smiley_on" if self.chatViewController.expandSmileys else "smiley_off"))

    def validateToolbarButton(self, item):
        """Called automatically by Cocoa in ChatWindowController"""

        tag = item.tag()

        if self.sessionController.hasStreamOfType("audio"):
            audio_stream = self.sessionController.streamHandlerOfType("audio")

        if self.sessionController.hasStreamOfType("video"):
            video_stream = self.sessionController.streamHandlerOfType("video")

        if tag==SessionController.TOOLBAR_RECONNECT and self.status in (STREAM_IDLE, STREAM_FAILED):
            return True
        elif tag == SessionController.TOOLBAR_AUDIO and self.status == STREAM_CONNECTED:
            if self.sessionController.hasStreamOfType("audio"):
                if audio_stream.status == STREAM_CONNECTED:
                    item.setToolTip_('Click to hangup the audio call')
                    item.setImage_(NSImage.imageNamed_("hangup"))
                    return True
                elif audio_stream.status == STREAM_PROPOSING or audio_stream.status == STREAM_RINGING:
                    item.setToolTip_('Click to cancel the audio call')
                    item.setImage_(NSImage.imageNamed_("hangup"))
                    return True
                else:
                    return False
            if self.sessionController.inProposal:
                return False
            return True
        elif tag == SessionController.TOOLBAR_RECORD:
            if self.sessionController.hasStreamOfType("audio"):
                if audio_stream.status == STREAM_CONNECTED:
                    if audio_stream.stream.recording_active:
                        item.setImage_(NSImage.imageNamed_("recording1"))
                    else:
                        item.setImage_(NSImage.imageNamed_("record"))
                    return True
                else:
                    item.setImage_(NSImage.imageNamed_("record"))
        elif tag == SessionController.TOOLBAR_HOLD and self.status == STREAM_CONNECTED:
            if self.sessionController.inProposal:
                return False
            if self.sessionController.hasStreamOfType("audio"):
                if audio_stream.status == STREAM_CONNECTED:
                    return True
                else:
                    return False
            else:
                return False
        elif tag == SessionController.TOOLBAR_VIDEO and self.status == STREAM_CONNECTED:
            return False
            # TODO: enable video -adi
            if self.sessionController.hasStreamOfType("video"):
                if video_stream.status == STREAM_CONNECTED:
                    item.setToolTip_('Click to hangup the video call')
                    item.setImage_(NSImage.imageNamed_("video-hangup"))
                    return True
                elif video_stream.status == STREAM_PROPOSING or video_stream.status == STREAM_RINGING:
                    item.setToolTip_('Click to cancel the video call')
                    item.setImage_(NSImage.imageNamed_("video-hangup"))
                    return True
                else:
                    return False
            if self.sessionController.inProposal:
                return False
            return True
        elif tag == SessionController.TOOLBAR_SEND_FILE and self.status == STREAM_CONNECTED:
            return True
        elif self.status==STREAM_CONNECTED and tag in (SessionController.TOOLBAR_DESKTOP_SHARING_BUTTON, SessionController.TOOLBAR_SHARE_DESKTOP_MENU, SessionController.TOOLBAR_REQUEST_DESKTOP_MENU):
            if self.sessionController.inProposal or self.sessionController.hasStreamOfType("desktop-sharing") or self.sessionController.remote_focus:
                return False
            return True
        elif tag == SessionController.TOOLBAR_SMILEY and self.status == STREAM_CONNECTED:
            return True
        elif tag == SessionController.TOOLBAR_HISTORY:
            return True
        return False

    def userClickedToolbarButton(self, sender):
        """
        Called by ChatWindowController when dispatching toolbar button clicks to the selected Session tab
        """
        tag = sender.tag()

        if self.sessionController.hasStreamOfType("audio"):
            audio_stream = self.sessionController.streamHandlerOfType("audio")

        if self.sessionController.hasStreamOfType("video"):
            video_stream = self.sessionController.streamHandlerOfType("video")

        if tag == SessionController.TOOLBAR_AUDIO:
            if self.status == STREAM_CONNECTED:
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
        elif tag == SessionController.TOOLBAR_RECORD:
            if audio_stream.stream.recording_active:
                audio_stream.stream.stop_recording()
                sender.setImage_(NSImage.imageNamed_("record"))
            else:
                settings = SIPSimpleSettings()
                session = self.sessionController.session
                direction = session.direction
                remote = "%s@%s" % (session.remote_identity.uri.user, session.remote_identity.uri.host)
                filename = "%s-%s-%s.wav" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), remote, direction)
                path = os.path.join(settings.audio.directory.normalized, session.account.id)
                audio_stream.stream.start_recording(os.path.join(path, filename))
                sender.setImage_(NSImage.imageNamed_("recording1"))
        elif tag == SessionController.TOOLBAR_HOLD:
            if self.status == STREAM_CONNECTED and self.sessionController.hasStreamOfType("audio") and not self.sessionController.inProposal:
                if audio_stream.holdByLocal:
                    audio_stream.unhold()
                    audio_stream.view.setSelected_(True)
                    sender.setImage_(NSImage.imageNamed_("pause"))
                else:
                    sender.setImage_(NSImage.imageNamed_("paused"))
                    audio_stream.hold()
        elif tag == SessionController.TOOLBAR_VIDEO:
            if self.status == STREAM_CONNECTED:
                if self.sessionController.hasStreamOfType("video"):
                    if video_stream.status == STREAM_PROPOSING or video_stream.status == STREAM_RINGING:
                        self.sessionController.cancelProposal(video_stream)
                    else:
                        self.sessionController.removeVideoFromSession()

                    sender.setToolTip_('Click to add video to this session')
                    sender.setImage_(NSImage.imageNamed_("video"))

                    # The button will be enabled again after operation is finished
                    sender.setEnabled_(False)
                else:
                    self.sessionController.addVideoToSession()
                    sender.setToolTip_('Click to cancel the video call')
                    sender.setImage_(NSImage.imageNamed_("video-hangup"))
        elif tag == SessionController.TOOLBAR_SEND_FILE:
            openFileTransferSelectionDialog(self.sessionController.account, self.sessionController.session.remote_identity.uri)
        elif tag == SessionController.TOOLBAR_RECONNECT:
            if self.status in (STREAM_IDLE, STREAM_FAILED):
                log_info(self, "Re-establishing session to %s" % self.remoteParty)
                self.sessionController.mustShowDrawer = True
                self.sessionController.startChatSession()
        elif tag == SessionController.TOOLBAR_SMILEY:
            self.chatViewController.expandSmileys = not self.chatViewController.expandSmileys
            sender.setImage_(NSImage.imageNamed_("smiley_on" if self.chatViewController.expandSmileys else "smiley_off"))
            self.chatViewController.toggleSmileys(self.chatViewController.expandSmileys)
        elif tag == SessionController.TOOLBAR_HISTORY:
            contactWindow = self.sessionController.owner
            contactWindow.showChatTranscripts_(None)
            if self.sessionController.account is BonjourAccount():
                contactWindow.transcriptViewer.filterByContactAccount('bonjour', self.sessionController.account)
            else:
                contactWindow.transcriptViewer.filterByContactAccount(format_identity(self.sessionController.target_uri), self.sessionController.account)

        elif tag == SessionController.TOOLBAR_SHARE_DESKTOP_MENU:
            if self.status == STREAM_CONNECTED:
                self.sessionController.addMyDesktopToSession()
                sender.setEnabled_(False)

        elif tag == SessionController.TOOLBAR_REQUEST_DESKTOP_MENU:
            if self.status == STREAM_CONNECTED:
                self.sessionController.addRemoteDesktopToSession()
                sender.setEnabled_(False)

    def remoteBecameIdle_(self, timer):
        window = ChatWindowManager.ChatWindowManager().windowForChatSession(self.sessionController)
        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
        self.remoteTypingTimer = None
        if window:
            window.noteSession_isComposing_(self.sessionController, False)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_ChatStreamGotMessage(self, sender, data):
        message = data.message
        icon = NSApp.delegate().windowController.iconPathForURI(format_identity_address(message.sender))

        hash = hashlib.sha1()
        hash.update(message.body.encode("utf-8")+str(message.timestamp))
        msgid = hash.hexdigest()

        if msgid not in self.history_msgid_list:
            name = format_identity(message.sender)
            recipient = message.recipients[0]
            recipient_uri = '%s@%s' % (recipient.uri.user, recipient.uri.host)
            remote_uri = format_identity_address(self.sessionController.remotePartyObject)

            private = True if self.sessionController.remote_focus and self.stream.private_messages_allowed and recipient_uri != remote_uri else False
            recipient_html = '%s <%s@%s>' % (recipient.display_name, recipient.uri.user, recipient.uri.host)                            
            self.chatViewController.showMessage(msgid, 'incoming', name, icon, message.body, message.timestamp, is_private=private, recipient=recipient_html, state="delivered")

            window = self.chatViewController.outputView.window()
            window_is_key = window.isKeyWindow() if window else False

            # FancyTabViewSwitcher will set unfocused tab item views as Hidden
            if not window_is_key or self.chatViewController.view.isHiddenOrHasHiddenAncestor():
                # notify growl
                growl_data = TimestampedNotificationData()
                growl_data.sender = format_identity_address(message.sender)
                if message.content_type == 'text/html':
                    growl_data.content = html2txt(message.body[0:400])
                else:
                    growl_data.content = message.body[0:400]
                NotificationCenter().post_notification("GrowlGotChatMessage", sender=self, data=growl_data)

    def _NH_ChatStreamGotComposingIndication(self, sender, data):
        window = ChatWindowManager.ChatWindowManager().windowForChatSession(self.sessionController)
        if window:
            flag = data.state == "active"
            if flag:
                refresh = data.refresh if data.refresh is not None else 120

                if data.last_active is not None and (data.last_active - datetime.datetime.now() > datetime.timedelta(seconds=refresh)):
                    # message is old, discard it
                    return

                if self.remoteTypingTimer:
                    # if we don't get any indications in the request refresh, then we assume remote to be idle
                    self.remoteTypingTimer.setFireDate_(NSDate.dateWithTimeIntervalSinceNow_(refresh))
                else:
                    self.remoteTypingTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(refresh, self, "remoteBecameIdle:", None, False)
            else:
                if self.remoteTypingTimer:
                    self.remoteTypingTimer.invalidate()
                    self.remoteTypingTimer = None

            window.noteSession_isComposing_(self.sessionController, flag)

    def _NH_SIPSessionDidFail(self, sender, data):
        message = "Session failed: %s" % data.reason
        self.chatViewController.showSystemMessage(message, datetime.datetime.utcnow(), True)

    def _NH_MediaStreamDidStart(self, sender, data):
        log_info(self, "Chat stream started")
        self.changeStatus(STREAM_CONNECTED)
        if self.handler:
            self.handler.setConnected(self.stream)

        # needed to set the Audio button state after session has started
        self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self)

    def _NH_MediaStreamDidEnd(self, sender, data):
        log_info(self, "Chat stream ended")

        self.notification_center.remove_observer(self, sender=self.stream)
        self.stream = None
        self.handler = None

        if self.history:
            self.history.close()
            self.history = None

        self.changeStatus(STREAM_IDLE, self.sessionController.endingBy)

        window = ChatWindowManager.ChatWindowManager().windowForChatSession(self.sessionController)
        if window:
            window.noteSession_isComposing_(self.sessionController, False)

    def _NH_MediaStreamDidFail(self, sender, data):
        reason = 'Connection has been closed due to an encryption error' if data.reason == 'A TLS packet with unexpected length was received.' else data.reason
        log_info(self, "Chat stream failed: %s" % reason)
        self.chatViewController.showSystemMessage(reason, datetime.datetime.utcnow(), True)

        self.changeStatus(STREAM_FAILED, reason)
        self.notification_center.remove_observer(self, sender=self.stream)
        self.stream = None
        self.handler = None

        if self.history:
            self.history.close()
            self.history = None

        window = ChatWindowManager.ChatWindowManager().windowForChatSession(self.sessionController)
        if window:
            window.noteSession_isComposing_(self.sessionController, False)

    def closeTabView(self):
        self.chatViewController.close()
        self.removeFromSession()

