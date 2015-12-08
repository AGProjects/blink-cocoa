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
import potr
import potr.crypt
import potr.context

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
from ChatOTR import BlinkOtrAccount, ChatOtrSmp
from ChatViewController import MSG_STATE_DEFERRED, MSG_STATE_DELIVERED, MSG_STATE_FAILED
from HistoryManager import ChatHistory
from SmileyManager import SmileyManager
from util import allocate_autorelease_pool, format_identity_to_string, sipuri_components_from_string, run_in_gui_thread


MAX_MESSAGE_LENGTH = 1300


class MessageInfo(object):
    def __init__(self, msgid, call_id='', direction='outgoing', sender=None, recipient=None, timestamp=None, text=None, content_type=None, status=None, encryption='', otr=False):
        self.msgid = msgid
        self.direction = direction
        self.sender = sender
        self.recipient = recipient
        self.timestamp = timestamp
        self.text = text
        self.content_type = content_type
        self.status = status
        self.call_id = call_id
        self.encryption = encryption
        self.otr = otr


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

    otr_account = None
    chatOtrSmpWindow = None
    contact = None

    account = None
    target_uri = None
    routes = None
    queue = None
    queued_serial = 0

    windowController = None
    OTRNegotiationTimer = None
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

            # OTR stuff
            self.require_encryption = self.contact.contact.require_encryption if self.contact is not None else True
            self.otr_negotiation_in_progress = False
            self.previous_is_encrypted = False
            self.init_otr()
            self.chatOtrSmpWindow = ChatOtrSmp(self, 'sms')
            self.otr_has_been_initialized = False
            self.log_info('Using local account %s' % self.local_uri)

        return self


    def dealloc(self):
        # release OTR check window
        if self.OTRNegotiationTimer is not None and self.OTRNegotiationTimer.isValid():
            self.OTRNegotiationTimer.invalidate()
            self.OTRNegotiationTimer = None

        if self.chatOtrSmpWindow:
            self.chatOtrSmpWindow.close()
            self.chatOtrSmpWindow = None

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

    def userClickedEncryptionMenu_(self, sender):
        tag = sender.tag()
        ctx = self.otr_account.getContext(self.session_id)
        if tag == 3: # required
            self.require_encryption = not self.require_encryption
            if self.contact is not None:
                self.contact.contact.require_encryption = self.require_encryption
                self.contact.contact.save()

            self.otr_account.peer_options['REQUIRE_ENCRYPTION'] = self.require_encryption
            if self.require_encryption:
                self.propose_otr()

        elif tag == 4: # active
            if self.is_encrypted:
                ctx.disconnect()
                self.init_otr(disable_encryption=True)
                self.updateEncryptionWidgets()
            else:
                self.init_otr()
                self.propose_otr()
        elif tag == 5: # verified
            fingerprint = ctx.getCurrentKey()
            if fingerprint:
                otr_fingerprint_verified = self.otr_account.getTrust(self.remote_uri, str(fingerprint))
                if otr_fingerprint_verified:
                    self.otr_account.removeFingerprint(self.remote_uri, str(fingerprint))
                else:
                    self.otr_account.setTrust(self.remote_uri, str(fingerprint), 'verified')

        elif tag == 9: # SMP window
            self.chatOtrSmpWindow.show()

        elif tag == 10:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://www.cypherpunks.ca/otr/Protocol-v2-3.1.0.html"))

    def revalidateToolbar(self):
        pass

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
            if self.contact is not None:
                peer_options['REQUIRE_ENCRYPTION'] = self.contact.contact.require_encryption
            settings = SIPSimpleSettings()
            peer_options['ALLOW_V2'] = settings.chat.enable_encryption

        self.otr_account = BlinkOtrAccount(peer_options=peer_options)
        self.otr_account.loadTrusts()

    def isOutputFrameVisible(self):
        return True

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

    def insertSmiley_(self, sender):
        smiley = sender.representedObject()
        self.chatViewController.appendAttributedString_(smiley)

    def matchesTargetAccount(self, target, account):
        that_contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(target)
        this_contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(self.target_uri)
        return (self.target_uri==target or (this_contact and that_contact and this_contact==that_contact)) and self.account==account

    def setEncryptionState(self, ctx):
        if self.otr_negotiation_in_progress:
            if ctx.state > 0 or ctx.tagOffer == 2:
                if ctx.tagOffer == 2:
                    self.chatViewController.showSystemMessage('0', NSLocalizedString("Failed to enable OTR encryption", "Label"), ISOTimestamp.now(), False)
                    self.log_info('OTR negotiation failed')
                elif ctx.state == 1:
                    self.log_info('OTR negotiation succeeded')
                self.otr_negotiation_in_progress = False
                self.chatViewController.loadingTextIndicator.setStringValue_("")
                self.chatViewController.loadingProgressIndicator.stopAnimation_(None)

        if self.previous_is_encrypted != self.is_encrypted:
            self.previous_is_encrypted = self.is_encrypted
            fingerprint = str(ctx.getCurrentKey())
            self.log_info('Remote OTR fingerprint %s' % fingerprint)

        self.updateEncryptionWidgets()

    @property
    def otr_status(self):
        ctx = self.otr_account.getContext(self.session_id)
        finished = ctx.state == potr.context.STATE_FINISHED
        encrypted = finished or ctx.state == potr.context.STATE_ENCRYPTED
        trusted = encrypted and bool(ctx.getCurrentTrust())
        return (encrypted, trusted, finished)

    @property
    def is_encrypted(self):
        return self.otr_status[0]

    def updateEncryptionWidgets(self):
        if not self.windowController:
            return
        if self.is_encrypted:
            ctx = self.otr_account.getContext(self.session_id)
            fingerprint = ctx.getCurrentKey()
            otr_fingerprint_verified = self.otr_account.getTrust(self.remote_uri, str(fingerprint))
            if otr_fingerprint_verified:
                self.windowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-green"))
            else:
                if self.otr_account.getTrusts(self.remote_uri):
                    self.windowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-red"))
                    try:
                        self.new_fingerprints[str(fingerprint)]
                    except KeyError:
                        self.new_fingerprints[str(fingerprint)] = True
                        self.notify_changed_fingerprint()
                else:
                    self.windowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-orange"))
        else:
            self.windowController.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("unlocked-darkgray"))


    def notify_changed_fingerprint(self):
        log_text = NSLocalizedString("%s changed encryption fingerprint. Please verify it again.", "Label") % self.windowController.titleLong
        self.log_info(log_text)

        self.chatViewController.showSystemMessage(self.session_id, log_text, ISOTimestamp.now(), True)

        NSApp.delegate().contactsWindowController.speak_text(log_text)

        nc_title = NSLocalizedString("Encryption Warning", "Label")
        nc_subtitle = self.titleLong
        nc_body = NSLocalizedString("Encryption fingerprint has changed", "Label")
        NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)

    def propose_otr(self):
        ctx = self.otr_account.getContext(self.session_id)
        newmsg = ctx.sendMessage(potr.context.FRAGMENT_SEND_ALL_BUT_LAST, '?OTRv2?', appdata={'stream':self})
        self.otr_negotiation_in_progress = True
        self.setEncryptionState(ctx)
        self.chatViewController.loadingTextIndicator.setStringValue_(NSLocalizedString("Negotiating Encryption...", "Label"))
        self.chatViewController.loadingProgressIndicator.startAnimation_(None)
        self.log_info(u"OTR negotiation started")
        if self.OTRNegotiationTimer is None:
            self.OTRNegotiationTimer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(8, self, "resetOTRTimer:", None, False)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.OTRNegotiationTimer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.OTRNegotiationTimer, NSEventTrackingRunLoopMode)

        self.send_message(newmsg, timestamp=ISOTimestamp.now())

    def resetOTRTimer_(self, timer):
        self.OTRNegotiationTimer.invalidate()
        self.OTRNegotiationTimer = None
        if self.otr_negotiation_in_progress:
            self.otr_negotiation_in_progress = False
            self.log_info('OTR negotiation timeout')
            #self.chatViewController.showSystemMessage('0', NSLocalizedString("Remote party does not support encryption", "Label"), ISOTimestamp.now(), False)
            self.chatViewController.loadingTextIndicator.setStringValue_("")
            self.chatViewController.loadingProgressIndicator.stopAnimation_(None)

    def gotMessage(self, sender, call_id, text, is_html=False, is_replication_message=False, timestamp=None, window=None):
        self.enableIsComposing = True
        icon = NSApp.delegate().contactsWindowController.iconPathForURI(format_identity_to_string(sender))
        timestamp = timestamp or ISOTimestamp.now()

        hash = hashlib.sha1()
        hash.update(text.encode('utf-8')+str(timestamp)+str(sender))
        msgid = hash.hexdigest()
        encryption = ''

        try:
            ctx = self.otr_account.getContext(self.session_id)
            text, tlvs = ctx.receiveMessage(text.encode('utf-8'), appdata={'stream': self})
            self.setEncryptionState(ctx)
            fingerprint = ctx.getCurrentKey()
            if fingerprint:
                otr_fingerprint_verified = self.otr_account.getTrust(self.remote_uri, str(fingerprint))
                if otr_fingerprint_verified:
                    encryption = 'verified'
                else:
                    encryption = 'unverified'

            self.chatOtrSmpWindow.handle_tlv(tlvs)

            if text is None:
                return

        except potr.context.NotOTRMessage, e:
            pass
        except potr.context.UnencryptedMessage, e:
            encryption = 'failed'
            status = 'failed'
            log = NSLocalizedString("Message %s is not encrypted, while encryption was expected", "Label") % msgid
            self.log_info(log)
            self.chatViewController.showSystemMessage(call_id, log, ISOTimestamp.now(), True)
        except potr.context.NotEncryptedError, e:
            encryption = 'failed'
            # we got some encrypted data
            log = NSLocalizedString("Encrypted message %s is unreadable, as encryption is disabled", "Label") % msgid
            status = 'failed'
            self.log_info(log)
            self.chatViewController.showSystemMessage(call_id, log, ISOTimestamp.now(), True)
            return
        except potr.context.ErrorReceived, e:
            status = 'failed'
            # got a protocol error
            log = 'Encrypted message %s protocol error: %s' % (msgid, e.args[0].error)
            self.log_info(log)
            #self.chatViewController.showSystemMessage(call_id, log, ISOTimestamp.now(), True)
            return
        except potr.crypt.InvalidParameterError, e:
            encryption = 'failed'
            status = 'failed'
            # received a packet we cannot process (probably tampered or
            # sent to wrong session)
            log = 'Invalid encrypted message %s received' % msgid
            self.log_info(log)
        #self.showSystemMessage(call_id, log, ISOTimestamp.now(), True)
        except RuntimeError, e:
            encryption = 'failed'
            status = 'failed'
            self.log_info('Encrypted message has runtime error: %s' % e)

        if text.startswith('?OTR:'):
            return

        if not is_replication_message and not window.isKeyWindow():
            if is_html:
                nc_body = html2txt(text.decode('utf-8'))
            else:
                nc_body = text.decode('utf-8')

            nc_title = NSLocalizedString("SMS Message Received", "Label")
            nc_subtitle = format_identity_to_string(sender, format='full')
            NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)

        encryption_log = " with encryption %s" % encryption if encryption else ''
        self.log_info(u"Incoming message %s received%s" % (call_id, encryption_log))

        self.chatViewController.showMessage(call_id, msgid, 'incoming', format_identity_to_string(sender), icon, text, timestamp, is_html=is_html, state="delivered", media_type='sms', encryption=encryption)

        self.notification_center.post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=NotificationData(direction='incoming', history_entry=False, remote_party=format_identity_to_string(sender), local_party=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour.local', check_contact=True))

        # save to history
        if not is_replication_message:
            message = MessageInfo(msgid, call_id=call_id, direction='incoming', sender=sender, recipient=self.account, timestamp=timestamp, text=text, content_type="html" if is_html else "text", status="delivered", encryption=encryption)
            self.add_to_history(message)

        if self.require_encryption and not self.otr_has_been_initialized:
            self.propose_otr()
            self.otr_has_been_initialized = True

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

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_DNSLookupDidFail(self, lookup, data):
        self.notification_center.remove_observer(self, sender=lookup)
        message = u"DNS lookup of SIP proxies for %s failed: %s" % (unicode(self.target_uri.host), data.error)
        self.setRoutesFailed(message)

    def _NH_DNSLookupDidSucceed(self, lookup, data):
        self.notification_center.remove_observer(self, sender=lookup)

        result_text = ', '.join(('%s:%s (%s)' % (result.address, result.port, result.transport.upper()) for result in data.result))
        self.log_info(u"DNS lookup for %s succeeded: %s" % (self.target_uri.host, result_text))
        routes = data.result
        if not routes:
            self.setRoutesFailed("No routes found to SIP Proxy")
        else:
            self.setRoutesResolved(routes)

    def _NH_SIPMessageDidSucceed(self, sender, data):
        try:
            message = self.messages.pop(str(sender))
        except KeyError:
            pass
        else:
            if self.routes:
                self.last_route = self.routes[0]
            call_id = data.headers['Call-ID'].body
            if message.otr:
                if data.code == 200:
                    ctx = self.otr_account.getContext(self.session_id)
                    self.setEncryptionState(ctx)
                else:
                    if self.otr_negotiation_in_progress:
                        self.chatViewController.loadingTextIndicator.setStringValue_("")
                        self.chatViewController.loadingProgressIndicator.stopAnimation_(None)
                        #self.chatViewController.showSystemMessage('0', NSLocalizedString("Remote party does not support encryption", "Label"), ISOTimestamp.now(), False)
                        self.log_info('OTR negotiation failed')
                        self.otr_negotiation_in_progress = False
            else:
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

    def _NH_SIPMessageDidFail(self, sender, data):
        try:
            message = self.messages.pop(str(sender))
        except KeyError:
            pass
        else:
            if data.code == 408:
                self.last_route = None

            call_id = message.call_id
            if message.otr:
                if self.otr_negotiation_in_progress:
                    self.chatViewController.loadingTextIndicator.setStringValue_("")
                    self.chatViewController.loadingProgressIndicator.stopAnimation_(None)
                    #self.chatViewController.showSystemMessage('0', NSLocalizedString("Remote party does not support encryption", "Label"), ISOTimestamp.now(), False)
                    self.log_info('OTR negotiation failed')
                    self.otr_negotiation_in_progress = False
            else:
                self.composeReplicationMessage(message, data.code)
                if message.content_type != "application/im-iscomposing+xml":
                    self.chatViewController.markMessage(message.msgid, MSG_STATE_FAILED)
                    message.status='failed'
                    self.add_to_history(message)
                    self.log_info(u"Outgoing message %s delivery failed: %s" % (call_id, data.reason))
        self.notification_center.remove_observer(self, sender=sender)

    @run_in_green_thread
    def add_to_history(self, message):
        # writes the record to the sql database
        cpim_to = format_identity_to_string(message.recipient) if message.recipient else ''
        cpim_from = format_identity_to_string(message.sender) if message.sender else ''
        cpim_timestamp = str(message.timestamp)
        content_type="html" if "html" in message.content_type else "text"

        self.history.add_message(message.msgid, 'sms', self.local_uri, self.remote_uri, message.direction, cpim_from, cpim_to, cpim_timestamp, message.text, content_type, "0", message.status, call_id=message.call_id)

    def composeReplicationMessage(self, sent_message, response_code):
        if isinstance(self.account, Account):
            if not self.account.sms.disable_replication:
                contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(self.target_uri)
                msg = CPIMPayload(sent_message.body.decode('utf-8'), sent_message.content_type, sender=ChatIdentity(self.account.uri, self.account.display_name), recipients=[ChatIdentity(self.target_uri, contact.name if contact else None)])
                self.sendReplicationMessage(response_code, msg.encode()[0], content_type='message/cpim')

    @run_in_green_thread
    def sendReplicationMessage(self, response_code, text, content_type="message/cpim", timestamp=None):
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
                                      RouteHeader(route.uri), content_type, text.encode('utf-8') if utf8_encode else text, credentials=self.account.credentials, extra_headers=extra_headers)
            message_request.send(15 if content_type != "application/im-iscomposing+xml" else 5)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def setRoutesResolved(self, routes):
        self.routes = routes
        for msgid, text, content_type in self.queue:
            self._sendMessage(msgid, text, content_type)
        self.queue = []

    @allocate_autorelease_pool
    @run_in_gui_thread
    def setRoutesFailed(self, msg):
        for msgid, text, content_type in self.queue:
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

    @run_in_green_thread
    def send_message(self, text, timestamp=None):
        # this function is only used by the OTR negotiator
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
                                      RouteHeader(routes[0].uri), content_type, text, credentials=self.account.credentials)
            self.notification_center.add_observer(self, sender=message_request)
            recipient = ChatIdentity(self.target_uri, self.display_name)
            hash = hashlib.sha1()
            hash.update(text.encode("utf-8")+str(timestamp))
            msgid = hash.hexdigest()
            id=str(message_request)
            self.messages[id] = MessageInfo(msgid, sender=self.account, recipient=recipient, timestamp=timestamp, content_type=content_type, text=text, otr=True)
            message_request.send(15)

    def _sendMessage(self, msgid, text, content_type="text/plain"):
        if content_type != "application/im-iscomposing+xml":
            self.enableIsComposing = True
            message = self.messages.pop(msgid)

            try:
                ctx = self.otr_account.getContext(self.session_id)
                newmsg = ctx.sendMessage(potr.context.FRAGMENT_SEND_ALL_BUT_LAST, message.text.encode('utf-8'), appdata={'stream': self})
                newmsg = newmsg.decode('utf-8')
                self.setEncryptionState(ctx)
                fingerprint = ctx.getCurrentKey()
                if fingerprint:
                    otr_fingerprint_verified = self.otr_account.getTrust(self.remote_uri, str(fingerprint))
                    if otr_fingerprint_verified:
                        message.encryption = 'verified'
                    else:
                        message.encryption = 'unverified'

                    self.chatViewController.updateEncryptionLock(msgid, message.encryption)

                route = self.last_route or self.routes[0]
                message_request = Message(FromHeader(self.account.uri, self.account.display_name), ToHeader(self.target_uri),
                                          RouteHeader(route.uri), content_type, newmsg, credentials=self.account.credentials)
                self.notification_center.add_observer(self, sender=message_request)
                message_request.send(15)
                message.status = 'sent'
                message.call_id = message_request._request.call_id
                self.log_info(u"Sending message %s" % (message_request._request.call_id))
                id=str(message_request)
                #self.no_report_received_messages[msgid] = message
                if 'has requested end-to-end encryption but this software does not support this feature' in newmsg:
                    self.log_info(u"Error sending message %s: OTR not started remotely" % msgid)
                    self.chatViewController.markMessage(message.msgid, MSG_STATE_FAILED)
                    message.status = 'failed'
                    self.chatViewController.showSystemMessage(self.session_id, NSLocalizedString("Remote party has not started OTR protocol", "Label"), ISOTimestamp.now(), True)
                    return
            except potr.context.NotEncryptedError, e:
                self.chatViewController.markMessage(message.msgid, MSG_STATE_FAILED)
                self.log_info('SMS message was not send. Either end your private OTR conversation, or restart it')
                return
        else:
            route = self.last_route or self.routes[0]
            message_request = Message(FromHeader(self.account.uri, self.account.display_name), ToHeader(self.target_uri),
                                      RouteHeader(route.uri), content_type, text, credentials=self.account.credentials)
            self.notification_center.add_observer(self, sender=message_request)
            message_request.send(5)
            id=str(message_request)
            message = MessageInfo(id, content_type=content_type, call_id=message_request._request.call_id)

        self.messages[id] = message

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

    def sendMessage(self, text, content_type="text/plain"):
        timestamp = ISOTimestamp.now()
        hash = hashlib.sha1()
        hash.update(text.encode("utf-8")+str(timestamp))
        msgid = hash.hexdigest()
        call_id = ''

        if content_type != "application/im-iscomposing+xml":
            icon = NSApp.delegate().contactsWindowController.iconPathForSelf()
            self.chatViewController.showMessage(call_id, msgid, 'outgoing', None, icon, text, timestamp, state="sent", media_type='sms')

            recipient = ChatIdentity(self.target_uri, self.display_name)
            self.messages[msgid] = MessageInfo(msgid, sender=self.account, recipient=recipient, timestamp=timestamp, content_type=content_type, text=text, status="queued")

        self.queue.append((msgid, text, content_type))

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
            text = unicode(textView.string())
            textView.setString_("")
            textView.didChangeText()

            if text:
                self.sendMessage(text)
            self.chatViewController.resetTyping()

            recipient = ChatIdentity(self.target_uri, self.display_name)
            self.notification_center.post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=NotificationData(direction='outgoing', history_entry=False, remote_party=format_identity_to_string(recipient), local_party=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour.local', check_contact=True))

            return True
        return False

    def textDidChange_(self, notif):
        chars_left = MAX_MESSAGE_LENGTH - self.chatViewController.inputText.textStorage().length()
        self.splitView.setText_(NSLocalizedString("%i chars left", "Label") % chars_left)

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
            if self.require_encryption and not self.otr_has_been_initialized:
                self.propose_otr()
                self.otr_has_been_initialized = True

    def chatViewDidLoad_(self, chatView):
         self.replay_history()

    def scroll_back_in_time(self):
         self.chatViewController.clear()
         self.chatViewController.resetRenderedMessages()
         self.replay_history()

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

    @allocate_autorelease_pool
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

        if not self.otr_negotiation_in_progress:
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


