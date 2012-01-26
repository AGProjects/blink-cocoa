# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *

import datetime
import hashlib

from application.notification import IObserver, NotificationCenter
from application.python import Null
from dateutil.tz import tzlocal
from zope.interface import implements

from sipsimple.account import Account, BonjourAccount
from sipsimple.core import Message, FromHeader, ToHeader, RouteHeader, Header, SIPURI
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.lookup import DNSLookup
from sipsimple.payloads.iscomposing import IsComposingDocument, IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.streams.applications.chat import CPIMMessage, CPIMIdentity
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import Timestamp, TimestampedNotificationData


from BlinkLogger import BlinkLogger
from ChatViewController import *
from HistoryManager import ChatHistory
from SmileyManager import SmileyManager
from SIPManager import SIPManager
from util import *

MAX_MESSAGE_LENGTH = 1300


class MessageInfo(object):
    def __init__(self, msgid, direction='outgoing', sender=None, recipient=None, timestamp=None, text=None, content_type=None, status=None):
        self.msgid = msgid 
        self.direction = direction
        self.sender = sender
        self.recipient = recipient
        self.timestamp = timestamp
        self.text = text
        self.content_type = content_type
        self.status = status
        
class SMSSplitView(NSSplitView):
    text = None
    attributes = NSDictionary.dictionaryWithObjectsAndKeys_(
                            NSFont.systemFontOfSize_(NSFont.labelFontSize()-1), NSFontAttributeName,
                            NSColor.darkGrayColor(), NSForegroundColorAttributeName)

    def setText_(self, text):
        self.text = NSString.stringWithString_(text)
        self.setNeedsDisplay_(True)

    def dividerThickness(self):
        return NSFont.labelFontSize()+1

    def drawDividerInRect_(self, rect):
        NSSplitView.drawDividerInRect_(self, rect)
        if self.text:
            point = NSMakePoint(NSMaxX(rect) - self.text.sizeWithAttributes_(self.attributes).width - 10, rect.origin.y)
            self.text.drawAtPoint_withAttributes_(point, self.attributes)


class SMSViewController(NSObject):
    implements(IObserver)

    chatViewController = objc.IBOutlet()
    splitView = objc.IBOutlet()
    smileyButton = objc.IBOutlet()
    outputContainer = objc.IBOutlet()
    addContactView = objc.IBOutlet()
    addContactLabel = objc.IBOutlet()

    showHistoryEntries = 50
    remoteTypingTimer = None
    enableIsComposing = False
    
    account = None
    target_uri = None
    routes = None
    queue = None
    queued_serial = 0

    def initWithAccount_target_name_(self, account, target, display_name):
        self = super(SMSViewController, self).init()
        if self:
            self.account = account
            self.target_uri = target
            self.display_name = display_name
            self.queue = []
            self.messages = {}

            self.history=ChatHistory()

            self.local_uri = '%s@%s' % (account.id.username, account.id.domain)
            self.remote_uri = '%s@%s' % (self.target_uri.user, self.target_uri.host)

            NSBundle.loadNibNamed_owner_("SMSView", self)

            self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("ChatView", "html"))
            self.chatViewController.setAccount_(self.account)
            self.chatViewController.resetRenderedMessages()

            self.chatViewController.inputText.unregisterDraggedTypes()
            self.chatViewController.inputText.setMaxLength_(MAX_MESSAGE_LENGTH)
            self.splitView.setText_("%i chars left" % MAX_MESSAGE_LENGTH)

            #if isinstance(self.account, Account) and not NSApp.delegate().windowController.hasContactMatchingURI(self.target_uri):
            #    self.enableAddContactPanel()
        return self

    def dealloc(self):
        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
        super(SMSViewController, self).dealloc()

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
                print "Can't load %s" % file
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

    def isOutputFrameVisible(self):
        return True

    def log_info(self, text):
        BlinkLogger().log_info(u"[Message to %s] %s" % (self.remote_uri, text))

    @objc.IBAction
    def addContactPanelClicked_(self, sender):
        if sender.tag() == 1:
            NSApp.delegate().windowController.addContact(self.target_uri)
        
        self.addContactView.removeFromSuperview()
        frame = self.chatViewController.outputView.frame()
        frame.origin.y = 0
        frame.size = self.outputContainer.frame().size
        self.chatViewController.outputView.setFrame_(frame)
    
    def enableAddContactPanel(self):
        text = u"%s is not in your contacts list. Would you like to add it now?" % format_identity_simple(self.target_uri)
        self.addContactLabel.setStringValue_(text)
    
        frame = self.chatViewController.outputView.frame()
        frame.size.height -= NSHeight(self.addContactView.frame())
        frame.origin.y += NSHeight(self.addContactView.frame())
        self.chatViewController.outputView.setFrame_(frame)
        self.outputContainer.addSubview_(self.addContactView)
        frame = self.addContactView.frame()
        frame.origin = NSZeroPoint
        self.addContactView.setFrame_(frame)

    def insertSmiley_(self, sender):
        smiley = sender.representedObject()
        self.chatViewController.appendAttributedString_(smiley)

    def matchesTargetAccount(self, target, account):
        that_contact = NSApp.delegate().windowController.getContactMatchingURI(target)
        this_contact = NSApp.delegate().windowController.getContactMatchingURI(self.target_uri)
        return (self.target_uri==target or (this_contact and that_contact and this_contact==that_contact)) and self.account==account

    def gotMessage(self, sender, message, is_html=False, state=None, timestamp=None):
        self.enableIsComposing = True
        icon = NSApp.delegate().windowController.iconPathForURI(format_identity_address(sender))
        timestamp = timestamp or Timestamp(datetime.datetime.now(tzlocal()))

        hash = hashlib.sha1()
        hash.update(message.encode('utf-8')+str(timestamp)+str(sender))
        msgid = hash.hexdigest()

        self.chatViewController.showMessage(msgid, 'incoming', format_identity(sender), icon, message, timestamp, is_html=is_html, state="delivered")

        NotificationCenter().post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity(sender), local_party=format_identity_address(self.account) if self.account is not BonjourAccount() else 'bonjour', check_contact=True))

        # save to history
        message = MessageInfo(msgid, direction='incoming', sender=sender, recipient=self.account, timestamp=timestamp, text=message, content_type="html" if is_html else "text", status="delivered")
        self.add_to_history(message)
 
    def remoteBecameIdle_(self, timer):
        window = timer.userInfo()
        if window:
            window.noteView_isComposing_(self, False)

        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
        self.remoteTypingTimer = None

    def gotIsComposing(self, window, state, refresh, last_active):
        self.enableIsComposing = True

        flag = state == "active"
        if flag:
            if refresh is None:
                refresh = 120

            if last_active is not None and (last_active - datetime.datetime.now(tzlocal()) > datetime.timedelta(seconds=refresh)):
                # message is old, discard it
                return

            if self.remoteTypingTimer:
                # if we don't get any indications in the request refresh, then we assume remote to be idle
                self.remoteTypingTimer.setFireDate_(NSDate.dateWithTimeIntervalSinceNow_(refresh))
            else:
                self.remoteTypingTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(refresh, self, "remoteBecameIdle:", window, False)
        else:
            if self.remoteTypingTimer:
                self.remoteTypingTimer.invalidate()
                self.remoteTypingTimer = None

        window.noteView_isComposing_(self, flag)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_SIPMessageDidSucceed(self, sender, data):
        BlinkLogger().log_info(u"SMS message delivery suceeded")

        self.composeReplicationMessage(sender, data.code)
        message = self.messages.pop(str(sender))

        if message.content_type != "application/im-iscomposing+xml":
            if data.code == 202:
                self.chatViewController.markMessage(message.msgid, MSG_STATE_DEFERRED)
                message.status='deferred'
            else:
                self.chatViewController.markMessage(message.msgid, MSG_STATE_DELIVERED)
                message.status='delivered'
            self.add_to_history(message)

        NotificationCenter().remove_observer(self, sender=sender)

    def _NH_SIPMessageDidFail(self, sender, data):
        BlinkLogger().log_info(u"SMS message delivery failed: %s" % data.reason)

        self.composeReplicationMessage(sender, data.code)
        message = self.messages.pop(str(sender))
        
        if message.content_type != "application/im-iscomposing+xml":
            self.chatViewController.markMessage(message.msgid, MSG_STATE_FAILED)
            message.status='failed'
            self.add_to_history(message)

        NotificationCenter().remove_observer(self, sender=sender)

    @run_in_green_thread
    def add_to_history(self, message):
        # writes the record to the sql database
        cpim_to = format_identity(message.recipient) if message.recipient else ''
        cpim_from = format_identity(message.sender) if message.sender else ''
        cpim_timestamp = str(message.timestamp)
        content_type="html" if "html" in message.content_type else "text"

        try:
            self.history.add_message(message.msgid, 'sms', self.local_uri, self.remote_uri, message.direction, cpim_from, cpim_to, cpim_timestamp, message.text, content_type, "0", message.status)
        except Exception, e:
            BlinkLogger().log_error(u"Failed to add message to history: %s" % e)

    def composeReplicationMessage(self, sent_message, response_code):
        if isinstance(self.account, Account):
            settings = SIPSimpleSettings()
            if settings.chat.sms_replication:
                contact = NSApp.delegate().windowController.getContactMatchingURI(self.target_uri)
                msg = CPIMMessage(sent_message.body.decode('utf-8'), sent_message.content_type, sender=CPIMIdentity(self.account.uri, self.account.display_name), recipients=[CPIMIdentity(self.target_uri, contact.display_name if contact else None)])
                self.sendReplicationMessage(response_code, str(msg), content_type='message/cpim')

    @run_in_green_thread
    def sendReplicationMessage(self, response_code, text, content_type="message/cpim", timestamp=None):
        timestamp = timestamp or datetime.datetime.now(tzlocal())
        # Lookup routes
        if self.account.sip.outbound_proxy is not None:
            uri = SIPURI(host=self.account.sip.outbound_proxy.host,
                         port=self.account.sip.outbound_proxy.port,
                         parameters={'transport': self.account.sip.outbound_proxy.transport})
        else:
            uri = SIPURI(host=self.account.id.domain)
        lookup = DNSLookup()
        settings = SIPSimpleSettings()
        try:
            routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list).wait()
        except DNSLookupError:
            pass
        else:
            utf8_encode = content_type not in ('application/im-iscomposing+xml', 'message/cpim')
            extra_headers = [Header("X-Offline-Storage", "no"), Header("X-Replication-Code", str(response_code)), Header("X-Replication-Timestamp", str(Timestamp(datetime.datetime.now())))]
            message_request = Message(FromHeader(self.account.uri, self.account.display_name), ToHeader(self.account.uri),
                                      RouteHeader(routes[0].get_uri()), content_type, text.encode('utf-8') if utf8_encode else text, credentials=self.account.credentials, extra_headers=extra_headers)
            message_request.send(15 if content_type != "application/im-iscomposing+xml" else 5)

    def setRoutesResolved(self, routes):
        self.routes = routes
        for msgid, text, content_type in self.queue:
            self._sendMessage(msgid, text, content_type)
        self.queue = []

    def setRoutesFailed(self, msg):
        BlinkLogger().log_error(u"DNS Lookup failed: %s" % msg)
        self.chatViewController.showSystemMessage("Cannot send SMS message to %s\n%s" % (self.target_uri, msg))
        for msgid, text, content_type in self.queue:
            message = self.messages.pop(msgid)
            if content_type not in ('application/im-iscomposing+xml', 'message/cpim'):
                message.status='failed'
                self.add_to_history(message)
        self.queue = []

    def _sendMessage(self, msgid, text, content_type="text/plain"):

        utf8_encode = content_type not in ('application/im-iscomposing+xml', 'message/cpim')
        message_request = Message(FromHeader(self.account.uri, self.account.display_name), ToHeader(self.target_uri),
                                  RouteHeader(self.routes[0].get_uri()), content_type, text.encode('utf-8') if utf8_encode else text, credentials=self.account.credentials)
        NotificationCenter().add_observer(self, sender=message_request)
        message_request.send(15 if content_type!="application/im-iscomposing+xml" else 5)

        id=str(message_request)
        if content_type != "application/im-iscomposing+xml":
            BlinkLogger().log_info(u"Sent %s SMS message to %s" % (content_type, self.target_uri))
            self.enableIsComposing = True
            message = self.messages.pop(msgid)
            message.status='sent'
        else:
            message = MessageInfo(id, content_type=content_type)

        self.messages[id] = message
        return message

    def sendMessage(self, text, content_type="text/plain"):
        SIPManager().lookup_sip_proxies(self.account, self.target_uri, self)

        timestamp = Timestamp(datetime.datetime.now(tzlocal()))
        hash = hashlib.sha1()
        hash.update(text.encode("utf-8")+str(timestamp))
        msgid = hash.hexdigest()
 
        if content_type != "application/im-iscomposing+xml":
            icon = NSApp.delegate().windowController.iconPathForSelf()
            self.chatViewController.showMessage(msgid, 'outgoing', None, icon, text, timestamp, state="sent")
        
            recipient=CPIMIdentity(self.target_uri, self.display_name)
            self.messages[msgid] = MessageInfo(msgid, sender=self.account, recipient=recipient, timestamp=timestamp, content_type=content_type, text=text, status="queued")

        self.queue.append((msgid, text, content_type))

    def textView_doCommandBySelector_(self, textView, selector):
        if selector == "insertNewline:" and self.chatViewController.inputText == textView:
            text = unicode(textView.string())
            textView.setString_("")
            textView.didChangeText()

            if text:
                self.sendMessage(text)
            self.chatViewController.resetTyping()

            recipient=CPIMIdentity(self.target_uri, self.display_name)
            NotificationCenter().post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=TimestampedNotificationData(direction='outgoing', history_entry=False, remote_party=format_identity(recipient), local_party=format_identity_address(self.account) if self.account is not BonjourAccount() else 'bonjour', check_contact=True))

            return True
        return False

    def textDidChange_(self, notif):
        chars_left = MAX_MESSAGE_LENGTH - self.chatViewController.inputText.textStorage().length()
        self.splitView.setText_("%i chars left" % chars_left)

    def getContentView(self):
        return self.chatViewController.view

    def chatView_becameIdle_(self, chatView, last_active):
        if self.enableIsComposing:
            content = IsComposingMessage(state=State("idle"), refresh=Refresh(60), last_active=LastActive(last_active or datetime.now()), content_type=ContentType('text')).toxml()
            self.sendMessage(content, IsComposingDocument.content_type)

    def chatView_becameActive_(self, chatView, last_active):
        if self.enableIsComposing:
            content = IsComposingMessage(state=State("active"), refresh=Refresh(60), last_active=LastActive(last_active or datetime.now()), content_type=ContentType('text')).toxml()
            self.sendMessage(content, IsComposingDocument.content_type)

    def chatViewDidLoad_(self, chatView):
         self.replay_history()

    @run_in_green_thread
    def replay_history(self):
        try:
            results = self.history.get_messages(local_uri=self.local_uri, remote_uri=self.remote_uri, media_type='sms', count=self.showHistoryEntries)
        except Exception, e:
            BlinkLogger().log_error(u"Failed to retrive sms history for %s: %s" % (self.remote_uri, e))
            return
        messages = [row for row in reversed(list(results))]
        self.render_history_messages(messages)

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

            self.chatViewController.showMessage(message.msgid, message.direction, message.cpim_from, icon, message.body, timestamp, recipient=message.cpim_to, state=message.status, is_html=is_html, history_entry=True)

    def webviewFinishedLoading_(self, notification):
        self.document = self.outputView.mainFrameDocument()
        self.finishedLoading = True
        for script in self.messageQueue:
            self.outputView.stringByEvaluatingJavaScriptFromString_(script)
        self.messageQueue = []

        if hasattr(self.delegate, "chatViewDidLoad_"):
            self.delegate.chatViewDidLoad_(self)

    def webView_decidePolicyForNavigationAction_request_frame_decisionListener_(self, webView, info, request, frame, listener):
        # intercept link clicks so that they are opened in Safari
        theURL = info[WebActionOriginalURLKey]
        if theURL.scheme() == "file":
            listener.use()
        else:
            listener.ignore()
            NSWorkspace.sharedWorkspace().openURL_(theURL)


