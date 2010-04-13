# Copyright (C) 2009 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *

import datetime

from application.notification import IObserver, NotificationCenter
from application.python.util import Null
from zope.interface import implements

from sipsimple.account import Account
from sipsimple.core import Message, FromHeader, ToHeader, RouteHeader, Header, SIPURI
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.lookup import DNSLookup
from sipsimple.payloads.iscomposing import IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.streams.applications.chat import CPIMMessage, CPIMIdentity
from sipsimple.util import run_in_green_thread

from BlinkLogger import BlinkLogger
from BlinkHistory import BlinkHistory
from ChatViewController import *
from SmileyManager import SmileyManager
from SIPManager import SIPManager
from util import *


MAX_MESSAGE_LENGTH = 1300


class SMSSplitView(NSSplitView):
    text = None
    attributes = NSDictionary.dictionaryWithObjectsAndKeys_(
                            NSFont.systemFontOfSize_(NSFont.labelFontSize()-1), NSFontAttributeName,
                            NSColor.darkGrayColor(), NSForegroundColorAttributeName)

    def setText_(self, text):
        self.text = NSString.alloc().initWithString_(text)
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
    upperContainer = objc.IBOutlet()
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
    history = None

    incoming_queue = None

    def initWithAccount_target_name_(self, account, target, display_name):
        self = super(SMSViewController, self).init()
        if self:
            self.account = account
            self.target_uri = target
            self.display_name = display_name
            self.queue = []
            self.incoming_queue = []

            NSBundle.loadNibNamed_owner_("SMSView", self)

            try:
                self.history = BlinkHistory().open_sms_history(self.account, format_identity_address(self.target_uri))
                self.chatViewController.setHistory_(self.history)
            except Exception, exc:
                import traceback
                traceback.print_exc()
                self.loggingEnabled = False
                self.chatViewController.writeSysMessage("Unable to create SMS history file: %s"%exc)

            self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("sms", "html"))
            self.chatViewController.setAccount_(self.account)

            self.chatViewController.inputText.unregisterDraggedTypes()
            self.chatViewController.inputText.setMaxLength_(MAX_MESSAGE_LENGTH)
            self.splitView.setText_("%i chars left" % MAX_MESSAGE_LENGTH)

            if isinstance(self.account, Account) and not NSApp.delegate().windowController.hasContactMatchingURI(self.target_uri):
                self.enableAddContactPanel()
        return self

    def dealloc(self):
        if self.history:
            self.history.close()
            self.history = None
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
    
    @objc.IBAction
    def addContactPanelClicked_(self, sender):
        if sender.tag() == 1:
            NSApp.delegate().windowController.addContact(self.target_uri)
        
        self.addContactView.removeFromSuperview()
        frame = self.chatViewController.outputView.frame()
        frame.origin.y = 0
        frame.size = self.upperContainer.frame().size
        self.chatViewController.outputView.setFrame_(frame)
    
    def enableAddContactPanel(self):
        text = u"%s is not in your contacts list. Would you like to add it now?" % format_identity_simple(self.target_uri)
        self.addContactLabel.setStringValue_(text)
    
        frame = self.chatViewController.outputView.frame()
        frame.size.height -= NSHeight(self.addContactView.frame())
        frame.origin.y += NSHeight(self.addContactView.frame())
        self.chatViewController.outputView.setFrame_(frame)
        self.upperContainer.addSubview_(self.addContactView)
        frame = self.addContactView.frame()
        frame.origin = NSZeroPoint
        self.addContactView.setFrame_(frame)

    def insertSmiley_(self, sender):
        smiley = sender.representedObject()
        self.chatViewController.appendAttributedString_(smiley)

    def setRoutesResolved(self, routes):
        self.routes = routes
        if self.queue:
            BlinkLogger().log_info("Sending queued SMS messages...")
        for msgid, msg, content_type in self.queue:
            nmsgid = self.doSendMessage(msg, content_type)
            self.chatViewController.setMessageSent("-%s" % msgid, nmsgid)
        self.queue = []

    def setRoutesFailed(self, msg):
        BlinkLogger().log_error("DNS Lookup failed: %s" % msg)
        self.chatViewController.writeSysMessage("Cannot send SMS message to %s\n%s" % (self.target_uri, msg))

    def matchesTargetAccount(self, target, account):
        that_contact = NSApp.delegate().windowController.getContactMatchingURI(target)
        this_contact = NSApp.delegate().windowController.getContactMatchingURI(self.target_uri)
        return (self.target_uri==target or (this_contact and that_contact and this_contact==that_contact)) and self.account==account

    def gotMessage(self, sender, message, is_html=False):
        self.enableIsComposing = True
        icon = NSApp.delegate().windowController.iconPathForURI(format_identity_address(sender))
        if self.incoming_queue is not None:
            self.incoming_queue.append(("", format_identity(sender), icon, message, datetime.datetime.utcnow(), is_html))
        else:
            self.chatViewController.showMessage("", format_identity(sender), icon, message, datetime.datetime.utcnow(), is_html)

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

            if last_active is not None and (last_active - datetime.datetime.now() > datetime.timedelta(seconds=refresh)):
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
        if (data.code == 202):
            self.chatViewController.markMessage(str(sender), MSG_STATE_DEFERRED)
        else:
            self.chatViewController.markMessage(str(sender), MSG_STATE_DELIVERED)
        NotificationCenter().remove_observer(self, sender=sender)

    def _NH_SIPMessageDidFail(self, sender, data):
        BlinkLogger().log_warning("SMS message delivery failed: %s" % data.reason)
        self.chatViewController.markMessage(str(sender), MSG_STATE_FAILED)        
        NotificationCenter().remove_observer(self, sender=sender)

    def doSendMessage(self, text, content_type="text/plain"):
        if content_type != "application/im-iscomposing+xml":
            BlinkLogger().log_info("Sent %s SMS message to %s" % (content_type, self.target_uri))
            self.enableIsComposing = True

        utf8_encode = content_type not in ('application/im-iscomposing+xml', 'message/cpim')
        message_request = Message(FromHeader(self.account.uri, self.account.display_name), ToHeader(self.target_uri),
                                  RouteHeader(self.routes[0].get_uri()), content_type, text.encode('utf-8') if utf8_encode else text, credentials=self.account.credentials)
        NotificationCenter().add_observer(self, sender=message_request)
        message_request.send(14 if content_type!="application/im-iscomposing+xml" else 4)
        return str(message_request)

    @run_in_green_thread
    def sendReplicationMessage(self, text, content_type="message/cpim"):
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
            BlinkLogger().log_info("Sending replication SMS message to %s" % self.account.uri)
            extra_headers = [Header("X-Offline-Storage", "no")]
            message_request = Message(FromHeader(self.account.uri, self.account.display_name), ToHeader(self.account.uri),
                                      RouteHeader(routes[0].get_uri()), content_type, text.encode('utf-8') if utf8_encode else text, credentials=self.account.credentials, extra_headers=extra_headers)
            message_request.send(14 if content_type != "application/im-iscomposing+xml" else 4)

    def sendMessage(self, text, content_type="text/plain"):
        SIPManager().request_routes_lookup(self.account, self.target_uri, self)
        self.queued_serial += 1
        self.queue.append((self.queued_serial, text, content_type))

        # Send the MESSAGE again, this time back to myself
        if isinstance(self.account, Account):
            settings = SIPSimpleSettings()
            if settings.chat.sms_replication:
                contact = NSApp.delegate().windowController.getContactMatchingURI(self.target_uri)
                msg = CPIMMessage(text, content_type, sender=CPIMIdentity(self.account.uri, self.account.display_name), recipients=[CPIMIdentity(self.target_uri, contact.display_name if contact else None)])
                self.sendReplicationMessage(str(msg), 'message/cpim')

        return "-%s" % self.queued_serial

    def textView_doCommandBySelector_(self, textView, selector):
        if selector == "insertNewline:" and self.chatViewController.inputText == textView:
            text = unicode(textView.string())
            textView.setString_("")
            textView.didChangeText()

            if text:
                msgid = self.sendMessage(text)
                icon = NSApp.delegate().windowController.iconPathForSelf()
                self.chatViewController.showMessage(msgid, None, icon, text, datetime.datetime.utcnow())
            
            self.chatViewController.resetTyping()

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
            self.sendMessage(content, IsComposingMessage.content_type)

    def chatView_becameActive_(self, chatView, last_active):
        if self.enableIsComposing:
            content = IsComposingMessage(state=State("active"), refresh=Refresh(60), last_active=LastActive(last_active or datetime.now()), content_type=ContentType('text')).toxml()
            self.sendMessage(content, IsComposingMessage.content_type)

    def chatViewDidLoad_(self, chatView):
        if self.showHistoryEntries > 0:
            lines = BlinkHistory().get_sms_history(self.account, self.target_uri, self.showHistoryEntries)

            for entry in lines:
                timestamp = entry["send_time"] or entry["delivered_time"]
                sender = entry["sender"]
                text = entry["text"]
                is_html = entry["type"] == "html"
                address, display_name, full_uri, fancy_uri = format_identity_from_text(sender)
                icon = NSApp.delegate().windowController.iconPathForURI(address)
                chatView.writeOldMessage(None, sender, icon, text, timestamp, entry["state"], is_html)

        if self.incoming_queue is not None:
            for args in self.incoming_queue:
                self.chatViewController.showMessage(*args)

        self.incoming_queue = None

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


