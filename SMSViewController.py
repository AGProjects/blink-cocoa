# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSEventTrackingRunLoopMode,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSWorkspace)

from Foundation import (NSAttributedString,
                        NSBundle,
                        NSColor,
                        NSDate,
                        NSDictionary,
                        NSFont,
                        NSImage,
                        NSLocalizedString,
                        NSMakePoint,
                        NSMakeSize,
                        NSMaxX,
                        NSMenuItem,
                        NSObject,
                        NSRunLoopCommonModes,
                        NSRunLoop,
                        NSSplitView,
                        NSString,
                        NSTimer,
                        NSWorkspace,
                        NSURL)
import objc
import uuid

from WebKit import WebActionOriginalURLKey

import datetime
import hashlib

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.system import host
from zope.interface import implements

from sipsimple.account import Account, BonjourAccount
from sipsimple.core import Message, FromHeader, ToHeader, RouteHeader, Header, SIPURI
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.payloads.iscomposing import IsComposingDocument, IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.streams.msrp.chat import CPIMPayload, ChatIdentity
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp


from BlinkLogger import BlinkLogger
from ChatViewController import MSG_STATE_DEFERRED, MSG_STATE_DELIVERED, MSG_STATE_FAILED
from HistoryManager import ChatHistory
from SmileyManager import SmileyManager
from util import format_identity_to_string, html2txt, sipuri_components_from_string, run_in_gui_thread


MAX_MESSAGE_LENGTH = 1300


class MessageInfo(object):
    def __init__(self, msgid, call_id='', direction='outgoing', sender=None, recipient=None, timestamp=None, content=None, content_type=None, status=None):
        self.msgid = msgid
        self.direction = direction
        self.sender = sender
        self.recipient = recipient
        self.timestamp = timestamp
        self.content = content
        self.content_type = content_type
        self.status = status
        self.call_id = call_id


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
    zoom_period_label = ''

    showHistoryEntries = 50
    remoteTypingTimer = None
    enableIsComposing = False
    handle_scrolling = True
    scrollingTimer = None
    scrolling_back = False
    message_count_from_history = 0

    contact = None

    account = None
    target_uri = None
    routes = None
    queue = None
    queued_serial = 0

    windowController = None
    last_route = None

    def initWithAccount_target_name_(self, account, target, display_name):
        self = objc.super(SMSViewController, self).init()
        if self:
            self.session_id = str(uuid.uuid1())

            self.notification_center = NotificationCenter()
            self.account = account
            self.target_uri = target
            self.display_name = display_name
            self.queue = []
            self.messages = {}

            self.history=ChatHistory()

            self.local_uri = '%s@%s' % (account.id.username, account.id.domain)
            self.remote_uri = '%s@%s' % (self.target_uri.user, self.target_uri.host)
            self.contact = NSApp.delegate().contactsWindowController.getFirstContactFromAllContactsGroupMatchingURI(self.remote_uri)

            NSBundle.loadNibNamed_owner_("SMSView", self)

            self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("ChatView", "html"))
            self.chatViewController.setAccount_(self.account)
            self.chatViewController.resetRenderedMessages()

            self.chatViewController.inputText.unregisterDraggedTypes()
            self.chatViewController.inputText.setMaxLength_(MAX_MESSAGE_LENGTH)
            self.splitView.setText_(NSLocalizedString("%i chars left", "Label") % MAX_MESSAGE_LENGTH)

            self.enableIsComposing = True

            self.log_info('Using local account %s' % self.local_uri)

        return self

    def dealloc(self):
        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
        self.chatViewController.close()
        objc.super(SMSViewController, self).dealloc()

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

    @objc.python_method
    def revalidateToolbar(self):
        pass

    @objc.python_method
    def isOutputFrameVisible(self):
        return True

    @objc.python_method
    def log_info(self, text):
        BlinkLogger().log_info(u"[SMS %s with %s] %s" % (self.session_id, self.remote_uri, text))

    @objc.IBAction
    def addContactPanelClicked_(self, sender):
        if sender.tag() == 1:
            NSApp.delegate().contactsWindowController.addContact(uris=[(self.target_uri, 'sip')])

        self.addContactView.removeFromSuperview()
        frame = self.chatViewController.outputView.frame()
        frame.origin.y = 0
        frame.size = self.outputContainer.frame().size
        self.chatViewController.outputView.setFrame_(frame)

    @objc.python_method
    def insertSmiley_(self, sender):
        smiley = sender.representedObject()
        self.chatViewController.appendAttributedString_(smiley)

    @objc.python_method
    def matchesTargetAccount(self, target, account):
        that_contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(target)
        this_contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(self.target_uri)
        return (self.target_uri==target or (this_contact and that_contact and this_contact==that_contact)) and self.account==account

    @objc.python_method
    def gotMessage(self, sender, call_id, content, is_html=False, is_replication_message=False, timestamp=None, window=None):
        self.enableIsComposing = True
        icon = NSApp.delegate().contactsWindowController.iconPathForURI(format_identity_to_string(sender))
        timestamp = timestamp or ISOTimestamp.now()

        hash = hashlib.sha1()
        hash.update(content.encode('utf-8')+str(timestamp)+str(sender))
        msgid = hash.hexdigest()
        encryption = ''

        if not is_replication_message and not window.isKeyWindow():
            if is_html:
                nc_body = html2txt(content.decode('utf-8'))
            else:
                nc_body = content.decode('utf-8')

            nc_title = NSLocalizedString("SMS Message Received", "Label")
            nc_subtitle = format_identity_to_string(sender, format='full')
            NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)

        self.log_info(u"Incoming message %s received" % call_id)

        self.chatViewController.showMessage(call_id, msgid, 'incoming', format_identity_to_string(sender), icon, content, timestamp, is_html=is_html, state="delivered", media_type='sms', encryption=encryption)

        self.notification_center.post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=NotificationData(direction='incoming', history_entry=False, remote_party=format_identity_to_string(sender), local_party=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour.local', check_contact=True))

        # save to history
        if not is_replication_message or (is_replication_message and self.local_uri == self.account.id):
            message = MessageInfo(msgid, call_id=call_id, direction='incoming', sender=sender, recipient=self.account, timestamp=timestamp, content=content, content_type="html" if is_html else "text", status="delivered", encryption=encryption)
            self.add_to_history(message)

    def remoteBecameIdle_(self, timer):
        window = timer.userInfo()
        if window:
            window.noteView_isComposing_(self, False)

        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
        self.remoteTypingTimer = None

    @objc.python_method
    def gotIsComposing(self, window, state, refresh, last_active):
        self.enableIsComposing = True

        flag = state == "active"
        if flag:
            if refresh is None:
                refresh = 120

            if last_active is not None and (last_active - ISOTimestamp.now() > datetime.timedelta(seconds=refresh)):
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

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    @objc.python_method
    def _NH_DNSLookupDidFail(self, lookup, data):
        self.notification_center.remove_observer(self, sender=lookup)
        message = u"DNS lookup of SIP proxies for %s failed: %s" % (unicode(self.target_uri.host), data.error)
        self.setRoutesFailed(message)

    @objc.python_method
    def _NH_DNSLookupDidSucceed(self, lookup, data):
        self.notification_center.remove_observer(self, sender=lookup)

        result_text = ', '.join(('%s:%s (%s)' % (result.address, result.port, result.transport.upper()) for result in data.result))
        self.log_info(u"DNS lookup for %s succeeded: %s" % (self.target_uri.host, result_text))
        routes = data.result
        if not routes:
            self.setRoutesFailed("No routes found to SIP Proxy")
        else:
            self.setRoutesResolved(routes)

    @objc.python_method
    def _NH_SIPMessageDidSucceed(self, sender, data):
        try:
            message = self.messages.pop(str(sender))
        except KeyError:
            pass
        else:
            if self.routes:
                self.last_route = self.routes[0]
            call_id = data.headers['Call-ID'].body
            self.composeReplicationMessage(message, data.code)
            if message.content_type != "application/im-iscomposing+xml":
                self.log_info(u"Outgoing message %s delivered" % (call_id))
                if data.code == 202:
                    self.chatViewController.markMessage(message.msgid, MSG_STATE_DEFERRED)
                    message.status='deferred'
                else:
                    self.chatViewController.markMessage(message.msgid, MSG_STATE_DELIVERED)
                    message.status='delivered'
                message.call_id = call_id
                self.add_to_history(message)

        self.notification_center.remove_observer(self, sender=sender)

    @objc.python_method
    def _NH_SIPMessageDidFail(self, sender, data):
        try:
            message = self.messages.pop(str(sender))
        except KeyError:
            pass
        else:
            if data.code == 408:
                self.last_route = None

            call_id = message.call_id
            self.composeReplicationMessage(message, data.code)
            if message.content_type != "application/im-iscomposing+xml":
                self.chatViewController.markMessage(message.msgid, MSG_STATE_FAILED)
                message.status='failed'
                self.add_to_history(message)
                self.log_info(u"Outgoing message %s delivery failed: %s" % (call_id, data.reason))
        self.notification_center.remove_observer(self, sender=sender)

    @objc.python_method
    def add_to_history(self, message):
        # writes the record to the sql database
        cpim_to = format_identity_to_string(message.recipient) if message.recipient else ''
        cpim_from = format_identity_to_string(message.sender) if message.sender else ''
        cpim_timestamp = str(message.timestamp)
        content_type="html" if "html" in message.content_type else "text"

        self.history.add_message(message.msgid, 'sms', self.local_uri, self.remote_uri, message.direction, cpim_from, cpim_to, cpim_timestamp, message.content, content_type, "0", message.status, call_id=message.call_id)

    @objc.python_method
    def composeReplicationMessage(self, sent_message, response_code):
        if sent_message.content_type == "application/im-iscomposing+xml":
            return

        if isinstance(self.account, Account):
            if not self.account.sms.disable_replication:
                contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(self.target_uri)
                msg = CPIMPayload(sent_message.content.encode('utf-8'), sent_message.content_type, charset='utf-8', sender=ChatIdentity(self.account.uri, self.account.display_name), recipients=[ChatIdentity(self.target_uri, contact.name if contact else None)])
                self.sendReplicationMessage(response_code, msg.encode()[0], content_type='message/cpim')

    @objc.python_method
    @run_in_green_thread
    def sendReplicationMessage(self, response_code, content, content_type="message/cpim", timestamp=None):
        timestamp = timestamp or ISOTimestamp.now()
        # Lookup routes
        if self.account.sip.outbound_proxy is not None:
            uri = SIPURI(host=self.account.sip.outbound_proxy.host,
                         port=self.account.sip.outbound_proxy.port,
                         parameters={'transport': self.account.sip.outbound_proxy.transport})
        else:
            uri = SIPURI(host=self.account.id.domain)

        route = None
        if self.last_route is None:
            lookup = DNSLookup()
            settings = SIPSimpleSettings()
            try:
                routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list).wait()
            except DNSLookupError:
                pass
            else:
                route = routes[0]
        else:
            route = self.last_route

        if route:
            utf8_encode = content_type not in ('application/im-iscomposing+xml', 'message/cpim')
            extra_headers = [Header("X-Offline-Storage", "no"), Header("X-Replication-Code", str(response_code)), Header("X-Replication-Timestamp", str(ISOTimestamp.now()))]
            message_request = Message(FromHeader(self.account.uri, self.account.display_name), ToHeader(self.account.uri),
                                      RouteHeader(route.uri), content_type, content.encode('utf-8') if utf8_encode else content, credentials=self.account.credentials, extra_headers=extra_headers)
            message_request.send(15 if content_type != "application/im-iscomposing+xml" else 5)

    @objc.python_method
    @run_in_gui_thread
    def setRoutesResolved(self, routes):
        self.routes = routes
        for msgid, content, content_type in self.queue:
            self._sendMessage(msgid, content, content_type)
        self.queue = []

    @objc.python_method
    @run_in_gui_thread
    def setRoutesFailed(self, msg):
        for msgid, content, content_type in self.queue:
            try:
                message = self.messages.pop(msgid)
            except KeyError:
                pass
            else:
                if content_type not in ('application/im-iscomposing+xml', 'message/cpim'):
                    self.chatViewController.markMessage(message.msgid, MSG_STATE_FAILED)
                    message.status='failed'
                    self.add_to_history(message)
                    log_text =  NSLocalizedString("Routing failure: %s", "Label") % msg
                    self.chatViewController.showSystemMessage('0', msg, ISOTimestamp.now(), True)
                    self.log_info(log_text)
        self.queue = []

    @objc.python_method
    @run_in_green_thread
    def send_message(self, content, timestamp=None):
        # Lookup routes
        content_type = 'text/plain'
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
            message_request = Message(FromHeader(self.account.uri, self.account.display_name), ToHeader(self.target_uri),
                                      RouteHeader(routes[0].uri), content_type, content, credentials=self.account.credentials)
            self.notification_center.add_observer(self, sender=message_request)
            recipient = ChatIdentity(self.target_uri, self.display_name)
            hash = hashlib.sha1()
            hash.update(content.encode("utf-8")+str(timestamp))
            msgid = hash.hexdigest()
            id=str(message_request)
            self.messages[id] = MessageInfo(msgid, sender=self.account, recipient=recipient, timestamp=timestamp, content_type=content_type, content=content)
            message_request.send(15)

    @objc.python_method
    def _sendMessage(self, msgid, content, content_type="text/plain"):
        if content_type != "application/im-iscomposing+xml":
            self.enableIsComposing = True
            message = self.messages.pop(msgid)

            route = self.last_route or self.routes[0]
            message_request = Message(FromHeader(self.account.uri, self.account.display_name), ToHeader(self.target_uri),
                                      RouteHeader(route.uri), content_type, message.content.encode('utf-8'), credentials=self.account.credentials)
            self.notification_center.add_observer(self, sender=message_request)
            message_request.send(15)
            message.status = 'sent'
            message.call_id = message_request._request.call_id
            self.log_info(u"Sending message %s" % (message_request._request.call_id))
            id=str(message_request)
        else:
            route = self.last_route or self.routes[0]
            message_request = Message(FromHeader(self.account.uri, self.account.display_name), ToHeader(self.target_uri),
                                      RouteHeader(route.uri), content_type, content, credentials=self.account.credentials)
            self.notification_center.add_observer(self, sender=message_request)
            message_request.send(5)
            id=str(message_request)
            message = MessageInfo(id, content_type=content_type, call_id=message_request._request.call_id)

        self.messages[id] = message

    @objc.python_method
    def lookup_destination(self, target_uri):
        assert isinstance(target_uri, SIPURI)

        lookup = DNSLookup()
        self.notification_center.add_observer(self, sender=lookup)
        settings = SIPSimpleSettings()

        if isinstance(self.account, Account) and self.account.sip.outbound_proxy is not None:
            uri = SIPURI(host=self.account.sip.outbound_proxy.host, port=self.account.sip.outbound_proxy.port,
                         parameters={'transport': self.account.sip.outbound_proxy.transport})
            self.log_info(u"Starting DNS lookup for %s through proxy %s" % (target_uri.host, uri))
        elif isinstance(self.account, Account) and self.account.sip.always_use_my_proxy:
            uri = SIPURI(host=self.account.id.domain)
            self.log_info(u"Starting DNS lookup for %s via proxy of account %s" % (target_uri.host, self.account.id))
        else:
            uri = target_uri
            self.log_info(u"Starting DNS lookup for %s" % target_uri.host)
        lookup.lookup_sip_proxy(uri, settings.sip.transport_list)

    @objc.python_method
    def sendMessage(self, content, content_type="text/plain"):
        timestamp = ISOTimestamp.now()
        hash = hashlib.sha1()
        hash.update(content.encode("utf-8")+str(timestamp))
        msgid = hash.hexdigest()
        call_id = ''

        if content_type != "application/im-iscomposing+xml":
            icon = NSApp.delegate().contactsWindowController.iconPathForSelf()
            self.chatViewController.showMessage(call_id, msgid, 'outgoing', None, icon, content, timestamp, state="sent", media_type='sms')

            recipient = ChatIdentity(self.target_uri, self.display_name)
            self.messages[msgid] = MessageInfo(msgid, sender=self.account, recipient=recipient, timestamp=timestamp, content_type=content_type, content=content, status="queued")

        self.queue.append((msgid, content, content_type))

        # Async DNS lookup
        if host is None or host.default_ip is None:
            self.setRoutesFailed(NSLocalizedString("No Internet connection", "Label"))
            return

        if self.last_route is None:
            self.lookup_destination(self.target_uri)
        else:
            self.setRoutesResolved([self.last_route])

    def textView_doCommandBySelector_(self, textView, selector):
        if selector == "insertNewline:" and self.chatViewController.inputText == textView:
            content = unicode(textView.string())
            textView.setString_("")
            textView.didChangeText()

            if content:
                self.sendMessage(content)
            self.chatViewController.resetTyping()

            recipient = ChatIdentity(self.target_uri, self.display_name)
            self.notification_center.post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=NotificationData(direction='outgoing', history_entry=False, remote_party=format_identity_to_string(recipient), local_party=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour.local', check_contact=True))

            return True
        return False

    def textDidChange_(self, notif):
        chars_left = MAX_MESSAGE_LENGTH - self.chatViewController.inputText.textStorage().length()
        self.splitView.setText_(NSLocalizedString("%i chars left", "Label") % chars_left)

    @objc.python_method
    def getContentView(self):
        return self.chatViewController.view

    def chatView_becameIdle_(self, chatView, last_active):
        if self.enableIsComposing:
            content = IsComposingMessage(state=State("idle"), refresh=Refresh(60), last_active=LastActive(last_active or ISOTimestamp.now()), content_type=ContentType('text')).toxml()
            self.sendMessage(content, IsComposingDocument.content_type)

    def chatView_becameActive_(self, chatView, last_active):
        if self.enableIsComposing:
            content = IsComposingMessage(state=State("active"), refresh=Refresh(60), last_active=LastActive(last_active or ISOTimestamp.now()), content_type=ContentType('text')).toxml()
            self.sendMessage(content, IsComposingDocument.content_type)

    def chatViewDidLoad_(self, chatView):
         self.replay_history()

    @objc.python_method
    def scroll_back_in_time(self):
         self.chatViewController.clear()
         self.chatViewController.resetRenderedMessages()
         self.replay_history()

    @objc.python_method
    @run_in_green_thread
    def replay_history(self):
        blink_contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(self.target_uri)
        if not blink_contact:
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

        messages = [row for row in reversed(results)]
        self.render_history_messages(messages)

    @objc.python_method
    @run_in_gui_thread
    def render_history_messages(self, messages):
        if self.chatViewController.scrolling_zoom_factor:
            if not self.message_count_from_history:
                self.message_count_from_history = len(messages)
                self.chatViewController.lastMessagesLabel.setStringValue_(self.zoom_period_label)
            else:
                if self.message_count_from_history == len(messages):
                    self.chatViewController.setHandleScrolling_(False)
                    self.chatViewController.lastMessagesLabel.setStringValue_(NSLocalizedString("%s. There are no previous messages.", "Label") % self.zoom_period_label)
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
                elif delta.days <= 3650:
                    self.chatViewController.scrolling_zoom_factor = 7

        call_id = None
        seen_sms = {}
        last_media_type = 'sms'
        last_chat_timestamp = None
        for message in messages:
            if message.status == 'failed':
                continue

            if message.sip_callid != '' and message.media_type == 'sms':
                try:
                    seen = seen_sms[message.sip_callid]
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
            is_html = False if message.content_type == 'text' else True

            #if call_id is not None and call_id != message.sip_callid and message.media_type == 'chat':
            #   self.chatViewController.showSystemMessage(message.sip_callid, 'Chat session established', timestamp, False)

            #if message.media_type == 'sms' and last_media_type == 'chat':
            #   self.chatViewController.showSystemMessage(message.sip_callid, 'Short messages', timestamp, False)

            self.chatViewController.showMessage(message.sip_callid, message.msgid, message.direction, message.cpim_from, icon, message.body, timestamp, recipient=message.cpim_to, state=message.status, is_html=is_html, history_entry=True, media_type = message.media_type, encryption=message.encryption)

            call_id = message.sip_callid
            last_media_type = 'chat' if message.media_type == 'chat' else 'sms'
            if message.media_type == 'chat':
                last_chat_timestamp = timestamp

        self.chatViewController.loadingProgressIndicator.stopAnimation_(None)
        self.chatViewController.loadingTextIndicator.setStringValue_("")

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


