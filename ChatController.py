# Copyright (C) 2009 AG Projects. See LICENSE for details.     
#

from Foundation import *
from AppKit import *

import datetime
import time

from itertools import dropwhile, takewhile

from application import log
from application.notification import IObserver, NotificationCenter
from application.python.util import Null
from zope.interface import implements

from sipsimple.account import Account, BonjourAccount
from sipsimple.streams import ChatStream
from sipsimple.util import TimestampedNotificationData

import SessionController
import SessionManager

from BaseStream import *
from BlinkHistory import BlinkHistory
from BlinkLogger import BlinkLogger
from ChatViewController import *
from SIPManager import SIPManager
from SmileyManager import SmileyManager
from util import *


DELIVERY_TIMEOUT = 5.0
MAX_RESEND_LINES = 10

MAX_MESSAGE_LENGTH = 16*1024

class MessageInfo(object):
    def __init__(self, id, timestamp, state):
        self.id = id
        self.timestamp = timestamp
        self.state = state


class MessageHandler(NSObject):
    """
    Track what messages typed by local user were sent.

    Until the stream is connected, all messages typed will be queued and marked
    as queued. Once stream is connected, queued messages will be sent.

    Sent messages are marked as unconfirmed. In the UI they will not received
    any marking. As long as confirmation arrives, there will be no marking and
    the message will be removed from the internal queue.
    If no confirmation arrives until a timeout or a failed notification happens, 
    all unconfirmed and subsequent messages will be marked as undelivered in the UI
    until things are detected as normalized.
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

    def _send(self, text, timestamp):
        msgid = self.stream.send_message(text, timestamp=timestamp)
        message = MessageInfo(msgid, timestamp, MSG_STATE_SENDING)
        self.messages[msgid] = message
        return message

    def send(self, text):
        now = datetime.datetime.utcnow()
        icon = NSApp.delegate().windowController.iconPathForSelf()
        #if len(text) > MAX_MESSAGE_LENGTH:
        #    ret = NSRunAlertPanel("Message Too Long", 
        #          "The message you're attempting to send is too long to be sent at once.\n"
        #          "Do you want to send it in 16KB chunks?",
        #          "OK", "Cancel", None)
        #    if ret == NSAlertAlternateReturn:
        #        return False

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

            if self.connected:
                try:
                    message = self._send(text, now)
                except Exception, e:
                    log.err()
                    BlinkLogger().log_error("Error sending message: %s" % e)
                    self.delegate.writeSysMessage("Error sending message")
                else:
                    self.delegate.showMessage(message.id, None, icon, text, now)
            else:
                self.pending.append((text, now, "-" + str(now)))
                self.delegate.showMessage("-" + str(now), None, icon, text, now)
        return True

    def resend(self, text, msgid, state):
        now = datetime.datetime.utcnow()
        icon = NSApp.delegate().windowController.iconPathForSelf()
        if self.connected:
            try:
                message = self._send(text, now)
            except Exception, e:
                log.err()
                BlinkLogger().log_error("Error sending message: %s" % e)
                self.delegate.writeSysMessage("Error sending message")
            else:
                self.delegate.writeOldMessage(message.id, None, icon, text, message.timestamp, state, False)
        else:
            self.pending.append((text, now, msgid))
            self.delegate.showMessage(msgid, None, icon, text, now, state=state)

    def setStream(self, stream):
        self.stream = stream
        NotificationCenter().add_observer(self, sender=stream)

    def setConnected(self):
        self.connected = True
        for text, timestamp, msgid in self.pending:
            try:
                message = self._send(text, timestamp)
            except Exception, e:
                log.err()
                BlinkLogger().log_error("Error sending queued message: %s" % e)
            else:
                self.delegate.updateMessageId(msgid, message.id)
        self.pending = []

    def setDisconnected(self):
        self.connected = False
        for text, timestamp, msgid in self.pending:
            self.delegate.markMessage(msgid, MSG_STATE_FAILED)
        if self.stream:
            NotificationCenter().remove_observer(self, sender=self.stream)
            self.stream = None

    def shakyConnectionMode(self):
        return self.messages and time.time() - self.lastDeliveredTime >= DELIVERY_TIMEOUT or any(m for m in self.messages.itervalues() if m.state==MSG_STATE_FAILED)

    def markMessage(self, message, state):
        message.state = state
        self.delegate.markMessage(message.id, state)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_ChatStreamGotMessage(self, sender, data):
        message = data.message
        # display message
        name = format_identity(message.sender)
        icon = NSApp.delegate().windowController.iconPathForURI(format_identity_address(message.sender))
        self.delegate.showMessage(None, name, icon, message.body, datetime.datetime.utcnow())

        window = self.delegate.outputView.window()
        window_is_key = window.isKeyWindow() if window else False

        # FancyTabViewSwitcher will set unfocused tab item views as Hidden
        if not window_is_key or self.delegate.view.isHiddenOrHasHiddenAncestor():
            # notify growl
            growl_data = TimestampedNotificationData()
            growl_data.sender = format_identity_address(message.sender)
            if message.content_type == 'text/html':
                growl_data.content = html2txt(message.body[0:400])
            else:
                growl_data.content = message.body[0:400]
            NotificationCenter().post_notification("GrowlGotChatMessage", sender=self, data=growl_data)

    def _NH_ChatStreamDidDeliverMessage(self, sender, data):
        message = self.messages.pop(data.message_id)
        self.markMessage(message, MSG_STATE_DELIVERED)
        self.lastDeliveredTime = time.time()

    def _NH_ChatStreamDidNotDeliverMessage(self, sender, data):
        message = self.messages.pop(data.message_id)
        self.markMessage(message, MSG_STATE_FAILED)


class ChatController(BaseStream):
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
            notification_center = NotificationCenter()
            notification_center.add_observer(self, sender=stream)
            notification_center.add_observer(self, sender=self.sessionController.session)

            NSBundle.loadNibNamed_owner_("SessionChat", self)

            self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("chat", "html"))
            self.chatViewController.setAccount_(self.sessionController.account)

            self.handler = MessageHandler.alloc().initWithSession_(self.sessionController.session)
            self.handler.setDelegate(self.chatViewController)

            if self.loggingEnabled:
                try:
                    uri = format_identity_address(self.sessionController.remotePartyObject)
                    contact = NSApp.delegate().windowController.getContactMatchingURI(uri)
                    if contact:
                        uri = str(contact.uri)
                    self.history = BlinkHistory().open_chat_history(self.sessionController.account, uri)
                    self.chatViewController.setHistory_(self.history)
                except Exception, exc:
                    self.loggingEnabled = False
                    self.chatViewController.writeSysMessage("Unable to create Chat History file: %s"%exc)

            if isinstance(self.sessionController.account, Account) and not NSApp.delegate().windowController.hasContactMatchingURI(scontroller.target_uri):
                self.enableAddContactPanel()

        return self

    def awakeFromNib(self):
        # setup smiley popup 
        smileys = SmileyManager().get_smiley_list()

        menu = self.smileyButton.menu()
        while menu.numberOfItems() > 0:
            menu.removeItemAtIndex_(0)

        #self.chatViewController.inputText.setMaxLength_(MAX_MESSAGE_LENGTH)

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

    def setStream(self, stream, connected=False):
        self.stream = stream
        self.handler.setStream(stream)
        if connected:
            self.handler.setConnected()
        NotificationCenter().add_observer(self, sender=stream)

    def end(self, autoclose=False):
        log_info(self, "Ending session in %s"%self.status)
        status = self.status
        if status in (STREAM_IDLE, STREAM_FAILED):
            self.closeChatWindow()
        elif status != STREAM_DISCONNECTING:
            self.changeStatus(STREAM_DISCONNECTING)
            if status == STREAM_PROPOSING:
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
            self.chatViewController.writeSysMessage("Session established", datetime.datetime.utcnow())
        elif newstate == STREAM_DISCONNECTING:
            BlinkLogger().log_info("Ending session")
        elif newstate == STREAM_CANCELLING:
            BlinkLogger().log_info("Cancelling Chat Proposal")
        elif newstate == STREAM_IDLE:
            if self.status not in (STREAM_FAILED, STREAM_IDLE):
                BlinkLogger().log_info("Session ended (%s)"%fail_reason)
                if fail_reason == "remote":
                    close_message = "%s has left the conversation" % self.sessionController.getTitleShort()
                    self.chatViewController.writeSysMessage(close_message, datetime.datetime.utcnow())
                else:
                    self.chatViewController.writeSysMessage("Session ended", datetime.datetime.utcnow())
                ended = True
        elif newstate == STREAM_FAILED:
            if self.status not in (STREAM_FAILED, STREAM_IDLE):
                if fail_reason:
                    BlinkLogger().log_error("Session failed: %s" % fail_reason)
                    self.chatViewController.writeSysMessage("Session failed: %s" % fail_reason, datetime.datetime.utcnow())
                else:
                    BlinkLogger().log_error("Session failed")
                    self.chatViewController.writeSysMessage("Session failed", datetime.datetime.utcnow())
                ended = True
        self.status = newstate
        BaseStream.changeStatus(self, newstate, fail_reason)
        if ended and self.stream:
            if self.handler:
                self.handler.setDisconnected()
            # don't close history here as it would not allow us to log queued messages anymore
            self.removeFromSession()

    def startOutgoing(self, is_update):
        SessionManager.SessionManager().showChatSession(self.sessionController)
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_WAITING_DNS_LOOKUP)

    def startIncoming(self, is_update):
        SessionManager.SessionManager().showChatSession(self.sessionController)
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_WAITING_DNS_LOOKUP)

    def closeChatWindow(self):
        SessionManager.SessionManager().removeFromSessionWindow(self.sessionController)

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
                if not self.handler.send(text):
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
            uri = format_identity_address(self.sessionController.remotePartyObject)
            contact = NSApp.delegate().windowController.getContactMatchingURI(uri)
            if contact:
                uri = str(contact.uri)
            entries = BlinkHistory().get_chat_history(self.sessionController.account, uri, self.showHistoryEntries)
            failed_entries = list(takewhile(lambda entry: entry['state']=='failed', reversed(entries)))
            old_entries = list(dropwhile(lambda entry: entry['state']=='failed', reversed(entries)))
            if len(failed_entries) > MAX_RESEND_LINES:
                r = NSRunAlertPanel("Chat", "There are %i entries that could not be delivered in previous sessions.\nWould you like to resend them?" % len(failed_entries),
                                    "Resend", "Cancel", None)
                if r != NSAlertDefaultReturn:
                    for entry in failed_entries:
                        entry['state'] = 'dropped'
                    old_entries = failed_entries + old_entries
                    failed_entries = []
            
            failed_entries.reverse()
            old_entries.reverse()
            
            for entry in old_entries:
                timestamp = entry["send_time"] or entry["delivered_time"]
                sender = entry["sender"]
                text = entry["text"]
                is_html = entry["type"] == "html"
                if entry["direction"] == 'send':
                    icon = NSApp.delegate().windowController.iconPathForSelf()
                else:
                    icon = NSApp.delegate().windowController.iconPathForURI(entry["sender_uri"])
                chatView.writeOldMessage(None, sender, icon, text, timestamp, entry["state"], is_html)
        else:
            failed_entries = []
        if self.sessionController.account is not BonjourAccount():
            # do not resend bonjour messages as they might arive at the wrong recipient who broadcasted the same address
            pending = failed_entries
            if self.history:
                pending += self.history.pending
                self.history.pending = []

            for entry in pending:
                self.handler.resend(entry["text"], entry["id"], entry["state"])

    def chatViewDidGetNewMessage_(self, chatView):
        NSApp.delegate().noteNewMessage(self.chatViewController.outputView.window())
        window = SessionManager.SessionManager().windowForChatSession(self.sessionController)
        if window:
            window.noteNewMessageForSession_(self.sessionController)

    def updateToolbarButtons(self, toolbar):
        for item in toolbar.visibleItems():
            if item.tag() == SessionController.TOOLBAR_SMILEY:
                item.setImage_(NSImage.imageNamed_("smiley_on" if self.chatViewController.expandSmileys else "smiley_off")) 
                break
            if item.tag() == SessionController.TOOLBAR_DESKTOP_SHARING:
                item.setEnabled_(self.status==STREAM_CONNECTED and not self.sessionController.inProposal)
                title = self.sessionController.getTitleShort()
                menu = toolbar.delegate().desktopShareMenu
                menu.itemWithTag_(SessionController.TOOLBAR_REQUEST_DESKTOP).setTitle_("Request Desktop from %s" % title)
                menu.itemWithTag_(SessionController.TOOLBAR_SHARE_DESKTOP).setTitle_("Share My Desktop with %s" % title)

    def validateToolbarButton(self, item):
        tag = item.tag()
        if tag==SessionController.TOOLBAR_CHAT and self.status in (STREAM_IDLE, STREAM_FAILED):
            if self.sessionController.inProposal:
                return False
            return True
        elif tag==SessionController.TOOLBAR_CALL and self.status==STREAM_CONNECTED and not self.sessionController.hasStreamOfType("audio"):
            if self.sessionController.inProposal:
                return False
            return True
        elif tag in (SessionController.TOOLBAR_SHARE_DESKTOP, SessionController.TOOLBAR_REQUEST_DESKTOP):
            if self.sessionController.inProposal:
                return False
            if self.status==STREAM_CONNECTED and not self.sessionController.hasStreamOfType("desktop-sharing"):
                return True
        elif tag in (SessionController.TOOLBAR_HANGUP, SessionController.TOOLBAR_SEND_FILE):
            return True
        elif tag in (SessionController.TOOLBAR_SMILEY, SessionController.TOOLBAR_HISTORY):
            return True
        return False

    @classmethod
    def validateToolbarButtonWhileDisconnected(cls, sessionController, item):
        return item.tag() in (SessionController.TOOLBAR_CHAT, SessionController.TOOLBAR_HISTORY)

    def userClickedToolbarButton(self, sender):
        """
        Called by SessionWindowController when dispatching toolbar button clicks to the selected
        Session tab.
        """
        tag = sender.tag()
        if tag == SessionController.TOOLBAR_HANGUP:
            self.end(False)
        elif tag == SessionController.TOOLBAR_CALL:
            if self.status == STREAM_CONNECTED:
                self.sessionController.addAudioToSession()
        elif tag == SessionController.TOOLBAR_SEND_FILE:
            SessionManager.SessionManager().pickFileAndSendTo(self.sessionController.account, self.sessionController.session.remote_identity.uri)
        elif tag == SessionController.TOOLBAR_CHAT:
            if self.status in (STREAM_IDLE, STREAM_FAILED):
                log_info(self, "Re-establishing session to %s" % self.remoteParty)
                self.sessionController.startChatSession()
        elif tag == SessionController.TOOLBAR_SMILEY:
            self.chatViewController.expandSmileys = not self.chatViewController.expandSmileys
            sender.setImage_(NSImage.imageNamed_("smiley_on" if self.chatViewController.expandSmileys else "smiley_off")) 
        elif tag == SessionController.TOOLBAR_HISTORY:
            contactWindow = self.sessionController.owner
            contactWindow.showChatTranscripts_(None)
            contactWindow.transcriptViewer.filterByContactAccount(format_identity(self.sessionController.target_uri), self.sessionController.account)
        elif tag == SessionController.TOOLBAR_SHARE_DESKTOP:
            if self.status == STREAM_CONNECTED:
                self.sessionController.addMyDesktopToSession()
        elif tag == SessionController.TOOLBAR_REQUEST_DESKTOP:
            if self.status == STREAM_CONNECTED:
                self.sessionController.addRemoteDesktopToSession()

    @classmethod
    def userClickedToolbarButtonWhileDisconnected(cls, sessionController, sender):
        """
        Called by SessionWindowController when dispatching toolbar button clicks to the selected
        Session tab.
        """
        tag = sender.tag()
        if tag == SessionController.TOOLBAR_HANGUP:
            pass
        elif tag == SessionController.TOOLBAR_CALL:
            sessionController.startAudioSession()
        elif tag == SessionController.TOOLBAR_CHAT:
            BlinkLogger().log_info("Re-establishing session to %s" % sessionController.remoteParty)
            sessionController.startChatSession()
        elif tag == SessionController.TOOLBAR_HISTORY:
            contactWindow = sessionController.owner
            contactWindow.showChatTranscripts_(None)
            contactWindow.transcriptViewer.filterByContactAccount(format_identity(sessionController.target_uri), sessionController.account)

    def remoteBecameIdle_(self, timer):
        window = SessionManager.SessionManager().windowForChatSession(self.sessionController)
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

    def _NH_ChatStreamGotComposingIndication(self, sender, data):
        window = SessionManager.SessionManager().windowForChatSession(self.sessionController)
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

    def _NH_SIPSessionDidStart(self, sender, data):
        if self.handler:
            log_info(self, "Chat stream started")
            streams = [stream for stream in data.streams if isinstance(stream, ChatStream)]
            self.setStream(streams[0], connected=True)
            self.changeStatus(STREAM_CONNECTED)

    def _NH_SIPSessionDidEnd(self, sender, data):
        log_info(self, "Chat stream ended: %s" % self.stream)
        self.changeStatus(STREAM_IDLE, self.sessionController.endingBy)
        if self.wasRemoved:
            if self.history:
                self.history.close()
                self.history = None
        window = SessionManager.SessionManager().windowForChatSession(self.sessionController)
        if window:
            window.noteSession_isComposing_(self.sessionController, False)

    def _NH_SIPSessionDidFail(self, sender, data):
        log_info(self, "Chat stream failed: %s" % self.stream)
        self.changeStatus(STREAM_FAILED, self.fail_reason or data.reason)
        self.fail_reason = None
        if self.wasRemoved:
            if self.history:
                self.history.close()
                self.history = None
        window = SessionManager.SessionManager().windowForChatSession(self.sessionController)
        if window:
            window.noteSession_isComposing_(self.sessionController, False)

    def _NH_SIPSessionDidRenegotiateStreams(self, sender, data):
        if data.action == 'remove' and self.stream in data.streams:
            if self.fail_reason is not None:
                log_info(self, "Chat stream failed: %s" % self.fail_reason)
                self.fail_reason = None
            else:
                log_info(self, "Chat stream ended: %s" % self.stream)
            self.changeStatus(STREAM_IDLE, self.sessionController.endingBy)
            if self.wasRemoved:
                if self.history:
                    self.history.close()
                    self.history = None
            window = SessionManager.SessionManager().windowForChatSession(self.sessionController)
            if window:
                window.noteSession_isComposing_(self.sessionController, False)
        elif data.action == 'add' and self.handler:
            streams = [stream for stream in data.streams if isinstance(stream, ChatStream)]
            if self.stream == streams[0]:
                log_info(self, "Chat stream started")
                self.setStream(streams[0], connected=True)
                self.changeStatus(STREAM_CONNECTED)

    def _NH_MediaStreamDidFail(self, sender, data):
        self.fail_reason = data.reason

    def didRemove(self):
        self.chatViewController.close()
        self.removeFromSession()
        NotificationCenter().remove_observer(self, sender=self.stream)
        self.stream = None
        self.handler = None
        self.wasRemoved = True
        # if we were closed but the stream didn't end yet, defer the history closure
        # to until the stream ends. That is to avoid missing messages received after the chat tab was closed
        if self.status in (STREAM_FAILED, STREAM_IDLE):
            if self.history:
                self.history.close()
                self.history = None


