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
from binascii import unhexlify, hexlify
import ast
import re

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.queue import EventQueue
from application.system import host
from zope.interface import implementer

from sipsimple.account import Account, BonjourAccount
from sipsimple.core import Message, FromHeader, ToHeader, RouteHeader, Header, SIPURI, Route
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.payloads.iscomposing import IsComposingDocument, IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.payloads.imdn import IMDNDocument, DisplayNotification, DeliveryNotification
from sipsimple.streams.msrp.chat import CPIMPayload, SimplePayload, CPIMParserError, CPIMHeader, ChatIdentity, OTREncryption, CPIMNamespace
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp
from Crypto.PublicKey import RSA

from otr import OTRTransport, OTRState, SMPStatus
from otr.exceptions import IgnoreMessage, UnencryptedMessage, EncryptedMessageError, OTRError, OTRFinishedError

from BlinkLogger import BlinkLogger
from ChatViewController import MSG_STATE_SENDING, MSG_STATE_SENT, MSG_STATE_DEFERRED, MSG_STATE_DELIVERED, MSG_STATE_FAILED, MSG_STATE_DISPLAYED
from HistoryManager import ChatHistory
from SmileyManager import SmileyManager
from util import format_identity_to_string, html2txt, sipuri_components_from_string, run_in_gui_thread
from ChatOTR import ChatOtrSmp
import SMSWindowManager


MAX_MESSAGE_LENGTH = 16000


class MessageInfo(object):
    def __init__(self, id, content=None, content_type=None, call_id=None, direction='outgoing', sender=None, recipient=None, timestamp=None, status=None, encryption=None):
        self.id = id
        self.call_id = call_id
        self.direction = direction
        self.sender = sender
        self.recipient = recipient
        self.timestamp = timestamp
        self.content = content if isinstance(content, bytes) else content.encode()
        self.content_type = content_type
        self.status = status
        self.encryption = encryption


class OTRInternalMessage(MessageInfo):
    def __init__(self, content):
        super(OTRInternalMessage, self).__init__('OTR', content=content, content_type='text/plain')


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


@implementer(IObserver)
class SMSViewController(NSObject):

    chatViewController = objc.IBOutlet()
    splitView = objc.IBOutlet()
    smileyButton = objc.IBOutlet()
    outputContainer = objc.IBOutlet()
    addContactView = objc.IBOutlet()
    addContactLabel = objc.IBOutlet()
    zoom_period_label = ''

    showHistoryEntries = 50
    remoteTypingTimer = None
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
    chatOtrSmpWindow = None
    dns_lookup_in_progress = False
    last_failure_reason = None
    otr_negotiation_timer = None

    def initWithAccount_target_name_(self, account, target, display_name):
        self = objc.super(SMSViewController, self).init()
        if self:
            self.public_key = None
            self.private_key = None

            self.session_id = str(uuid.uuid1())

            self.notification_center = NotificationCenter()
            self.account = account
            self.target_uri = target
            self.display_name = display_name
            self.messages = {}

            self.encryption = OTREncryption(self)

            self.message_queue = EventQueue(self._send_message)
            self.read_queue = EventQueue(self._send_read_notification)

            self.history=ChatHistory()

            self.local_uri = '%s@%s' % (account.id.username, account.id.domain)
            self.remote_uri = '%s@%s' % (self.target_uri.user.decode(), self.target_uri.host.decode())
            self.contact = NSApp.delegate().contactsWindowController.getFirstContactFromAllContactsGroupMatchingURI(self.remote_uri)

            if self.contact and self.contact.contact.public_key:
                self.public_key = RSA.importKey(self.contact.contact.public_key)

            try:
                private_key = self.account.sms.private_key
            except AttributeError:
                pass
            else:
                if private_key:
                    try:
                        self.private_key = RSA.importKey(private_key)
                    except Exception as e:
                        self.log_info('Cannot import private key: %s' % str(e))

            NSBundle.loadNibNamed_owner_("SMSView", self)

            self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("ChatView", "html"))
            self.chatViewController.setAccount_(self.account)
            self.chatViewController.resetRenderedMessages()

            self.chatViewController.inputText.unregisterDraggedTypes()
            self.chatViewController.inputText.setMaxLength_(MAX_MESSAGE_LENGTH)
            self.splitView.setText_(NSLocalizedString("%i chars left", "Label") % MAX_MESSAGE_LENGTH)

            self.log_info('Using local account %s' % self.local_uri)

            self.notification_center.add_observer(self, name='ChatStreamOTREncryptionStateChanged')
            self.started = False
            self.read_queue_started = False

        return self

    @property
    def enableIsComposing(self):
        return self.account.sms.enable_composing
        
    @property
    def pending_outgoing_messages(self):
        return SMSWindowManager.SMSWindowManager().pending_outgoing_messages
        
    def dealloc(self):
        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()

        if self.encryption.active:
            self.stopEncryption()

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
                print("Can't load %s" % file)
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
        BlinkLogger().log_info("[SMS with %s] %s" % (self.remote_uri, text))

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
    def gotMessage(self, sender, id, call_id, content, content_type, is_replication_message=False, timestamp=None, window=None,  imdn_timestamp=None, account=None):
        is_html = content_type == 'text/html'
        encrypted = False

        try:
            content = self.encryption.otr_session.handle_input(content, content_type)
        except IgnoreMessage:
            self.log_info('OTR message %s received' % call_id)
            return None
        except UnencryptedMessage:
            self.log_info('OTR in use but unencrypted message received')
            encrypted = False
            encryption_active = True
        except EncryptedMessageError as e:
            self.log_info('OTP encrypted message error: %s' % str(e))
            return None
        except OTRFinishedError:
            self.chatViewController.showSystemMessage("0", "The other party finished encryption", ISOTimestamp.now(), is_error=True)
            self.log_info('OTR has finished')
            encrypted = False
            encryption_active = False
        except OTRError as e:
            self.log_info('OTP error: %s' % str(e))
            return None
        else:
            encrypted = encryption_active = self.encryption.active

        content = content.decode() if isinstance(content, bytes) else content
        
        if content.startswith('?OTR:'):
            self.log_info('Dropped OTR message that could not be decoded')
            self.chatViewController.showSystemMessage("0", "The other party stopped encryption", ISOTimestamp.now(), is_error=True)
            if self.encryption.active:
                self.stopEncryption()
  
            return None

        icon = NSApp.delegate().contactsWindowController.iconPathForURI(format_identity_to_string(sender))
        timestamp = timestamp or ISOTimestamp.now()

        self.log_info("Incoming message %s received (SIP Call-Id %s)" % (id, call_id))
        encryption = ''
        if encrypted:
            encryption = 'verified' if self.encryption.verified else 'unverified'

        if not is_replication_message and not window.isKeyWindow():
            nc_body = html2txt(content) if is_html else content
            nc_title = NSLocalizedString("SMS Message Received", "Label")
            nc_subtitle = format_identity_to_string(sender, format='full')
            NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)

        self.chatViewController.showMessage(call_id, id, 'incoming', format_identity_to_string(sender), icon, content, timestamp, is_html=is_html, state="sent", media_type='sms', encryption=encryption)
        
        if content.endswith('==') and self.private_key:
            pass
            #print('Decrypt message with RSA', content)
            #unhexed = unhexlify(content.encode())
            #print(unhexed)
            #d = self.private_key.decrypt(content)
            #print(d)

        self.notification_center.post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=NotificationData(id=id, direction='incoming', history_entry=False, remote_party=format_identity_to_string(sender), local_party=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour.local', check_contact=True))

        # save to history
        if not is_replication_message or (is_replication_message and self.local_uri == self.account.id):
            message = MessageInfo(id, call_id=call_id, direction='incoming', sender=sender, recipient=self.account, timestamp=timestamp, content=content, content_type=content_type, status=MSG_STATE_DELIVERED, encryption=encryption)
            self.add_to_history(message)

        if imdn_timestamp and account.sms.enable_imdn:
            self.sendIMDNNotification(id, ISOTimestamp.now(), event='delivered')
            
        self.read_queue.put(id)

    @objc.python_method
    def _send_read_notification(self, id):
        self.sendIMDNNotification(id, ISOTimestamp.now(), event='displayed')

    def remoteBecameIdle_(self, timer):
        window = timer.userInfo()
        if window:
            window.noteView_isComposing_(self, False)

        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
        self.remoteTypingTimer = None

    @objc.python_method
    def gotIsComposing(self, window, state, refresh, last_active):
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
    def inject_otr_message(self, data):
        messageObject = OTRInternalMessage(data)
        self.sendMessage(messageObject)

    @objc.python_method
    def _NH_DNSLookupDidFail(self, lookup, data):
        self.dns_lookup_in_progress = False
        self.notification_center.remove_observer(self, sender=lookup)
        message = "DNS lookup for %s failed" % self.target_uri.host.decode()
        self.setRoutesFailed(message)

    @objc.python_method
    def _NH_DNSLookupDidSucceed(self, lookup, data):
        self.dns_lookup_in_progress = False
        self.notification_center.remove_observer(self, sender=lookup)
        result_text = ', '.join(('%s:%s (%s)' % (result.address, result.port, result.transport.upper()) for result in data.result))
        self.log_info("DNS lookup for %s succeeded: %s" % (self.target_uri.host.decode(), result_text))
        routes = data.result
        if not routes:
            self.setRoutesFailed("No routes found to SIP Proxy")
        else:
            self.setRoutesResolved(routes)

    @objc.python_method
    def _NH_ChatStreamOTREncryptionStateChanged(self, stream, data):
        try:
            if data.new_state is OTRState.Encrypted:
                local_fingerprint = stream.encryption.key_fingerprint
                remote_fingerprint = stream.encryption.peer_fingerprint
                self.log_info("Chat encryption activated using OTR protocol")
                self.log_info("OTR local fingerprint %s" % local_fingerprint)
                self.log_info("OTR remote fingerprint %s" % remote_fingerprint)
                self.chatViewController.showSystemMessage("0", "Encryption enabled", ISOTimestamp.now())
            elif data.new_state is OTRState.Finished:
                self.log_info("Chat encryption deactivated")
                self.chatViewController.showSystemMessage("0", "Encryption deactivated", ISOTimestamp.now(), is_error=True)
            elif data.new_state is OTRState.Plaintext:
                self.log_info("Chat encryption deactivated")
                self.chatViewController.showSystemMessage("0", "Encryption deactivated", ISOTimestamp.now(), is_error=True)
        except:
            import traceback
            traceback.print_exc()

    @objc.python_method
    def _NH_SIPMessageDidSucceed(self, sender, data):
        self.notification_center.remove_observer(self, sender=sender)
        self.last_failure_reason = None
        call_id = data.headers['Call-ID'].body
        user_agent = data.headers.get('User-Agent', Null).body
        content_type = data.headers.get('Content-Type', Null).body
        client = data.headers.get('Client', Null).body
        server = data.headers.get('Server', Null).body
        entity = user_agent or server or client
        

        try:
            message = next(message for message in self.messages.values() if message.call_id == call_id)
        except StopIteration:
            try:
                (message_id, event, timestamp) = self.pending_outgoing_messages[str(sender)]
            except (KeyError, IndexError):
                pass
            else:
                if event in ('delivered', 'displayed'):
                    self.log_info('%s notification for %s was sent' % (event, message_id))
                    self.history.update_message_status(message_id, event)
            return

        try:
            del self.pending_outgoing_messages[str(sender)]
        except KeyError:
            pass
        
        message = self.messages.pop(message.id)

        if message.content_type == IsComposingDocument.content_type:
            return

        self.composeReplicationMessage(message, data.code)

        if message.id == 'OTR':
            self.log_info("OTR message %s delivered to %s" % (call_id, entity))
        else:
            if message.content_type not in (IsComposingDocument.content_type, IMDNDocument.content_type):
                if data.code == 202:
                    self.chatViewController.markMessage(message.id, MSG_STATE_DEFERRED)
                    message.status = MSG_STATE_DEFERRED
                    self.log_info("%s message %s for %s accepted by %s for later delivery (Call-Id %s)" % (message.content_type, message.id, message.recipient, entity, call_id))
                else:
                    self.chatViewController.markMessage(message.id, MSG_STATE_SENT)
                    message.status = MSG_STATE_SENT
                    self.log_info("%s message %s for %s accepted by %s (Call-Id %s)" % (message.content_type, message.id, message.recipient, entity, call_id))

            self.add_to_history(message)

    @objc.python_method
    def _NH_SIPMessageDidFail(self, sender, data):
        self.notification_center.remove_observer(self, sender=sender)
        message = None
        try:
            call_id = data.headers['Call-ID'].body
        except (AttributeError, KeyError):
            call_id = None
            self.started = False

        try:
            message = next(message for message in self.messages.values() if message.call_id == call_id)
        except StopIteration:
            try:
                (message_id, event, timestamp) = self.pending_outgoing_messages[str(sender)]
            except KeyError as e:
                self.log_info('Cannot find pending outgoing message %s' % str(sender))
            else:
                if event in ('sent'):
                    message = self.messages.pop(message_id)
                elif event in ('delivered', 'displayed'):
                    self.log_info('%s notification for message %s failed' % (event, message_id))
                    return
                else:
                    self.log_info('Cannot find message with SIP CALL-Id %s' % call_id)
                    return
        else:
            message = self.messages.pop(message.id)

        if not message:
            self.log_info('Cannot find failed message %s' % str(sender))

        if message.content_type in (IsComposingDocument.content_type, IMDNDocument.content_type):
            return

        if data.code == 408 or data.code >= 500:
            self.last_route = None
            self.started = False

        call_id = message.call_id
        reason = data.reason.decode() if isinstance(data.reason, bytes) else data.reason
        reason += ' (%s)' % data.code
        
        self.composeReplicationMessage(message, data.code)

        if message.id == 'OTR':
            self.log_info("OTR message %s failed: %s" % (call_id, reason))
        else:
            message.status = 'failed'
            if message.content_type not in (IsComposingDocument.content_type, IMDNDocument.content_type):
                self.log_info("Outgoing msessage %s delivery failed: %s" % (call_id, reason))
                self.chatViewController.markMessage(message.id, MSG_STATE_FAILED)
                self.add_to_history(message)

        if (data.code == 480 or 'not online' in reason) and reason != self.last_failure_reason:
            self.chatViewController.showSystemMessage('0', 'User not online', ISOTimestamp.now(), True)
        else:
            self.chatViewController.showSystemMessage('0', reason, ISOTimestamp.now(), True)

        if self.otr_negotiation_timer:
            self.otr_negotiation_timer.invalidate()
        self.otr_negotiation_timer = None

        self.last_failure_reason = reason

    @objc.python_method
    def update_message_status(self, msgid, status):
        self.chatViewController.markMessage(msgid, status)
        self.history.update_message_status(msgid, status)

    @objc.python_method
    def add_to_history(self, message):
        # writes the record to the sql database
        cpim_to = format_identity_to_string(message.recipient) if message.recipient else ''
        cpim_from = format_identity_to_string(message.sender) if message.sender else ''
        cpim_timestamp = str(message.timestamp)
        content_type="html" if "html" in message.content_type else "text"

        self.history.add_message(message.id, 'sms', self.local_uri, self.remote_uri, message.direction, cpim_from, cpim_to, cpim_timestamp, message.content.decode(), content_type, "0", message.status, call_id=message.call_id)

    @objc.python_method
    def composeReplicationMessage(self, sent_message, response_code):
        if sent_message.content_type == IsComposingDocument.content_type:
            return

        if isinstance(self.account, Account):
            if self.account.sms.enable_replication:
                contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(self.target_uri)
                msg = CPIMPayload(sent_message.content, sent_message.content_type, charset='utf-8', sender=ChatIdentity(self.account.uri, self.account.display_name), recipients=[ChatIdentity(self.target_uri, contact.name if contact else None)])
                self.sendReplicationMessage(response_code, msg.encode()[0], content_type='message/cpim')

    @objc.python_method
    def sendReplicationMessage(self, response_code, content, content_type="message/cpim", timestamp=None):
        return
        # TODO must be refactored
        timestamp = timestamp or ISOTimestamp.now()
        additional_sip_headers = [Header("X-Offline-Storage", "no"), Header("X-Replication-Code", str(response_code)), Header("X-Replication-Timestamp", str(ISOTimestamp.now()))]
        message_request = Message(FromHeader(self.account.uri, self.account.display_name), ToHeader(self.account.uri),
                                  RouteHeader(route.uri), content_type, content, credentials=self.account.credentials, extra_headers=additional_sip_headers)
        message_request.send(15 if content_type != IsComposingDocument.content_type else 5)


    @objc.python_method
    @run_in_green_thread
    def sendIMDNNotification(self, message_id, timestamp, event='delivered'):
        #self.log_info('Send %s notification for %s' % (event, message_id))
        notification = DisplayNotification('displayed') if event == 'displayed' else DeliveryNotification('delivered')

        content = IMDNDocument.create(message_id=message_id, datetime=timestamp, recipient_uri=self.target_uri, notification=notification)

        imdn_id = str(uuid.uuid4())
        ns = CPIMNamespace('urn:ietf:params:imdn', 'imdn')
        additional_headers = [CPIMHeader('Message-ID', ns, imdn_id)]
        
        payload = CPIMPayload(content,
                              IMDNDocument.content_type,
                              charset='utf-8',
                              sender=ChatIdentity(self.account.uri, self.account.display_name),
                              recipients=[ChatIdentity(self.target_uri, None)],
                              timestamp=ISOTimestamp.now(),
                              additional_headers=additional_headers)

        payload, content_type = payload.encode()

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
            
            is_ip_address = re.match("^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", uri.host.decode()) or ":" in uri.host.decode()
            if self.account is BonjourAccount() and is_ip_address:
                tls_name = self.account.sip.tls_name or uri.host
                transport = uri.transport.decode() if isinstance(uri.transport, bytes) else uri.transport
                transport = 'tls' if uri.secure else transport.lower()
                port = uri.port or (5061 if transport=='tls' else 5060)
                route = Route(address=uri.host, port=port, transport=transport, tls_name=tls_name or uri.host)
            else:
                target_uri = uri
                tls_name = target_uri.host.decode()
                if self.account is not BonjourAccount():
                   if self.account.id.domain == target_uri.host.decode():
                       tls_name = self.account.sip.tls_name or self.account.id.domain
                   elif "isfocus" in str(target_uri) and target_uri.host.decode().endswith(self.account.id.domain):
                       tls_name = self.account.conference.tls_name or self.account.sip.tls_name or self.account.id.domain
                else:
                   if "isfocus" in str(target_uri) and self.account.conference.tls_name:
                       tls_name = self.account.conference.tls_name

                if self.account.sip.outbound_proxy is not None:
                   proxy = self.account.sip.outbound_proxy
                   uri = SIPURI(host=proxy.host, port=proxy.port, parameters={'transport': proxy.transport})
                   tls_name = self.account.sip.tls_name or proxy.host
                   self.log_info("Starting DNS lookup for %s via proxy %s" % (target_uri.host.decode(), uri))
                elif self.account.sip.always_use_my_proxy:
                   uri = SIPURI(host=self.account.id.domain)
                   tls_name = self.account.sip.tls_name or self.account.id.domain
                   self.log_info("Starting DNS lookup for %s via proxy of account %s" % (target_uri.host.decode(), self.account.id))
                else:
                   uri = target_uri
                   self.log_info("Starting DNS lookup for %s" % target_uri.host.decode())
                

                try:
                    routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list, tls_name=tls_name).wait()
                except DNSLookupError as e:
                    self.log_info('Failed to send IMDN %s' % str(e))
                else:
                    route = routes[0]
        else:
            route = self.last_route

        if route:
            message_request = Message(FromHeader(self.account.uri, self.account.display_name), ToHeader(self.target_uri),
                                      RouteHeader(route.uri), "message/cpim", payload, credentials=self.account.credentials)
            self.notification_center.add_observer(self, sender=message_request)
            self.add_pending_outgoing_message(str(message_request), message_id, event)

            message_request.send(15)

    @objc.python_method
    def add_pending_outgoing_message(self, id, message_id, event):
        #self.log_info('Adding Pending message object %s %s' % (id, message_id))
        self.pending_outgoing_messages[id] = (message_id, event, datetime.datetime.now())
    
    @objc.python_method
    @run_in_gui_thread
    def setRoutesResolved(self, routes):
        self.routes = routes
        self.last_route = self.routes[0]
        self.connect()
    
    def connect(self):
        if self.started:
            return

        self.log_info('Using route %s' % self.last_route)
        self.started = True
        self.message_queue.start()

        if not self.encryption.active and self.account.sms.enable_otr:
            self.startEncryption()

    @objc.python_method
    @run_in_gui_thread
    def setRoutesFailed(self, reason):
        self.log_info('Routing failed: %s' % reason)
        self.chatViewController.showSystemMessage('0', reason, ISOTimestamp.now(), True)

        try:
            for msgObject in self.message_queue.queue.queue:
                try:
                    message = self.messages.pop(msgObject.id)
                except KeyError:
                    self.log_info('Cannot find message %s' % msgObject.id)
                else:
                    if message.content_type not in (IsComposingDocument.content_type, IMDNDocument.content_type):
                        self.chatViewController.markMessage(message.id, MSG_STATE_FAILED)
                        message.status='failed'
                        self.add_to_history(message)

        except Exception as e:
            self.log_info('Error in routes failed %s' % str(e))

        self.started = False
        self.message_queue.stop()

    @objc.python_method
    def _send_message(self, message):
        if (not self.last_route):
            self.log_info('No route found')
            return

        message.timestamp = ISOTimestamp.now()

        if not isinstance(message, OTRInternalMessage):
            try:
                content = self.encryption.otr_session.handle_output(message.content, message.content_type)
            except OTRError as e:
                if 'has ended the private conversation' in str(e):
                    self.log_info('Encryption has been disabled by remote party, please resend the message again')
                    self.chatViewController.showSystemMessage("0", "The other party stopped encryption", ISOTimestamp.now(), is_error=True)
                    self.stopEncryption()
                else:
                    self.log_info('Failed to encrypt outgoing message: %s' % str(e))
                return
            except OTRFinishedError:
                self.log_info('Encryption has been disabled by remote party, please resend the message again')
                self.chatViewController.showSystemMessage("0", "The other party finished encryption", ISOTimestamp.now(), is_error=True)
                self.stopEncryption()
                return

            if self.encryption.active and not content.startswith(b'?OTR:'):
                self.chatViewController.showSystemMessage("0", "The other party stopped encryption", ISOTimestamp.now(), is_error=True)
                self.stopEncryption()
                if message.content_type not in (IsComposingDocument.content_type, IMDNDocument.content_type):
                    self.chatViewController.markMessage(message.id, MSG_STATE_FAILED)
                return None
        else:
            content = message.content

        #self.log_info('Currently encrypted = %s' % self.encryption.active)

        timeout = 5
        timeout = 5 if message.content_type != IsComposingDocument.content_type else 15

        additional_cpim_headers = []
        if self.account.sms.enable_imdn and message.content_type != IsComposingDocument.content_type:
            ns = CPIMNamespace('urn:ietf:params:imdn', 'imdn')
            additional_cpim_headers = [CPIMHeader('Message-ID', ns, message.id)]
            additional_cpim_headers.append(CPIMHeader('Disposition-Notification', ns, 'positive-delivery, display'))

        additional_sip_headers = []

        if self.account.sms.use_cpim:
            if self.public_key and message.content_type != IsComposingDocument.content_type:
                encrypted_content = self.public_key.encrypt(content, 32)
                content = hexlify(encrypted_content[0])
                additional_sip_headers = [Header("Public Key", self.contact.contact.public_key_checksum)]

            payload = CPIMPayload(content,
                                  message.content_type,
                                  charset='utf-8',
                                  sender=ChatIdentity(self.account.uri, self.account.display_name),
                                  recipients=[ChatIdentity(self.target_uri, None)],
                                  timestamp=message.timestamp,
                                  additional_headers=additional_cpim_headers)

            payload, content_type = payload.encode()
        else:
            payload = content
            content_type = message.content_type

        message_request = Message(FromHeader(self.account.uri, self.account.display_name),
                                  ToHeader(self.target_uri),
                                  RouteHeader(self.last_route.uri),
                                  content_type,
                                  payload,
                                  credentials=self.account.credentials,
                                  extra_headers=additional_sip_headers)

      
        if message.content_type not in (IsComposingDocument.content_type, IMDNDocument.content_type):
            self.add_pending_outgoing_message(str(message_request), message.id, 'sent')

        self.notification_center.add_observer(self, sender=message_request)
        
        message_request.send(timeout)
        message.status = MSG_STATE_SENDING
        message.call_id = message_request._request.call_id.decode()

        self.messages[message.id] = message

        if not isinstance(message, OTRInternalMessage):
            if message.content_type != IsComposingDocument.content_type:
                if self.encryption.active:
                    self.log_info('%s encrypted message %s pending to %s (SIP Call-ID %s)' % (message.content_type, message.id, self.last_route.uri, message.call_id))
                else:
                    self.log_info('%s message %s pending to %s (SIP Call-ID %s) using object' % (message.content_type, message.id, self.last_route.uri, message.call_id, str(message_request)))
        else:
            self.log_info('OTR message %s sent' % message.call_id)

    @objc.python_method
    def lookup_destination(self, uri):
        if self.dns_lookup_in_progress:
            #self.log_info("Lookup destination for %s already in progress" % uri)
            return

        self.dns_lookup_in_progress = True

        self.log_info("Lookup destination for %s" % uri)

        is_ip_address = re.match("^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", uri.host.decode()) or ":" in uri.host.decode()

        if self.account is BonjourAccount() and is_ip_address:
            tls_name = self.account.sip.tls_name
            transport = uri.transport.decode() if isinstance(uri.transport, bytes) else uri.transport
            transport = 'tls' if uri.secure else transport.lower()
            port = uri.port or (5061 if transport=='tls' else 5060)
            self.last_route = Route(address=uri.host, port=port, transport=transport, tls_name=tls_name or uri.host)
            self.connect()
            return

        self.lookup_dns(uri)

    @objc.python_method
    @run_in_green_thread
    def lookup_dns(self, target_uri):
        settings = SIPSimpleSettings()
        lookup = DNSLookup()
        self.notification_center.add_observer(self, sender=lookup)

        tls_name = target_uri.host.decode()
        if self.account is not BonjourAccount():
            if self.account.id.domain == target_uri.host.decode():
                tls_name = self.account.sip.tls_name or self.account.id.domain
            elif "isfocus" in str(target_uri) and target_uri.host.decode().endswith(self.account.id.domain):
                tls_name = self.account.conference.tls_name or self.account.sip.tls_name or self.account.id.domain
        else:
            if "isfocus" in str(target_uri) and self.account.conference.tls_name:
                tls_name = self.account.conference.tls_name

        if self.account.sip.outbound_proxy is not None:
            proxy = self.account.sip.outbound_proxy
            uri = SIPURI(host=proxy.host, port=proxy.port, parameters={'transport': proxy.transport})
            tls_name = self.account.sip.tls_name or proxy.host
            self.log_info("Starting DNS lookup for %s via proxy %s" % (target_uri.host.decode(), uri))
        elif self.account.sip.always_use_my_proxy:
            uri = SIPURI(host=self.account.id.domain)
            tls_name = self.account.sip.tls_name or self.account.id.domain
            self.log_info("Starting DNS lookup for %s via proxy of account %s" % (target_uri.host.decode(), self.account.id))
        else:
            uri = target_uri
            self.log_info("Starting DNS lookup for %s" % target_uri.host.decode())

        lookup.lookup_sip_proxy(uri, settings.sip.transport_list, tls_name=tls_name)

    @objc.python_method
    def stopEncryption():
        self.log_info('Stopping OTR...')
        self.stopEncryption()
        self.notification_center.post_notification('OTREncryptionDidStop', sender=self)
    
    @objc.python_method
    def read_queue_start(self):
        if self.read_queue_started:
            #self.log_info('Read queue resume')
            self.read_queue.unpause()
            return
            
        #self.log_info('Started read queue')
        self.read_queue.start()
        self.read_queue_started = True

    @objc.python_method
    def read_queue_stop(self):
        #self.log_info('Read queue paused')
        self.read_queue.pause()

    @objc.python_method
    def sendMessage(self, content, content_type="text/plain"):
        # entry point for sending messages, they will be added to self.message_queue
        icon = NSApp.delegate().contactsWindowController.iconPathForSelf()

        if isinstance(content, OTRInternalMessage):
            self.message_queue.put(content)
            return

        timestamp = ISOTimestamp.now()
        content = content.decode() if isinstance(content, bytes) else content
        id = str(uuid.uuid4()) # use IMDN compatible id

        if self.encryption.active:
            encryption = 'verified' if self.encryption.verified else 'unverified'

        if content_type != IsComposingDocument.content_type:
            self.chatViewController.showMessage('', id, 'outgoing', None, icon, content, timestamp, state="sending", media_type='sms', encryption='')

        recipient = ChatIdentity(self.target_uri, self.display_name)
        mInfo = MessageInfo(id, sender=self.account, recipient=recipient, timestamp=timestamp, content_type=content_type, content=content, status="queued", encryption='')
    
        self.messages[id] = mInfo
        self.message_queue.put(mInfo)

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
            content = str(textView.string())
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
            if message.direction == 'incoming' and message.status != MSG_STATE_DISPLAYED:
                self.sendIMDNNotification(message.msgid, ISOTimestamp.now(), event='displayed')

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
            
            self.chatViewController.showMessage(message.sip_callid, message.id, message.direction, message.cpim_from, icon, message.body, timestamp, recipient=message.cpim_to, state=message.status, is_html=is_html, history_entry=True, media_type = message.media_type, encryption=message.encryption)

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

    @property
    def chatWindowController(self):
        return NSApp.delegate().chatWindowController

    @objc.python_method
    def startEncryption(self):
        self.encryption.start()
        self.otr_negotiation_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(15, self, "otrNegotiationTimeout:", None, False)
 
    def otrNegotiationTimeout_(self, timer):
        if not self.encryption.active:
            self.chatViewController.showSystemMessage("0", "The other party did not answer", ISOTimestamp.now(), is_error=True)

        if self.otr_negotiation_timer:
            self.otr_negotiation_timer.invalidate()
        self.otr_negotiation_timer = None

    @objc.IBAction
    def userClickedEncryptionMenu_(self, sender):
        tag = sender.tag()
        if tag == 1: # active
            if self.encryption.active:
                self.stopEncryption()
            else:
                self.startEncryption()
                
        elif tag == 5: # verified
            self.encryption.verified = not self.encryption.verified

        elif tag == 6: # SMP window
            if self.encryption.active:
                self.log_info('Show OTR window')
                #self.chatOtrSmpWindow.show()

        elif tag == 7:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("https://otr.cypherpunks.ca/Protocol-v3-4.0.0.html"))


OTRTransport.register(SMSViewController)
