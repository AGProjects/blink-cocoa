# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

__all__ = ['ChatInputTextView', 'ChatViewController', 'processHTMLText',
           'MSG_STATE_SENDING', 'MSG_STATE_FAILED', 'MSG_STATE_DELIVERED', 'MSG_STATE_DEFERRED']

from Foundation import *
from AppKit import *
from WebKit import WebViewProgressFinishedNotification, WebActionOriginalURLKey
from WebKit import WebDragDestinationActionDHTML, WebDragDestinationActionNone

import time
import cgi
import datetime
import calendar
import urllib
import os
import re

from application.notification import NotificationCenter
from sipsimple.util import TimestampedNotificationData

from SmileyManager import SmileyManager
from util import call_in_gui_thread, escape_html, format_identity, format_identity_address, format_identity_from_text


MSG_STATE_SENDING = "sending" # middleware told us the message is being sent
MSG_STATE_FAILED = "failed" # msg delivery failed
MSG_STATE_DELIVERED = "delivered" # msg successfully delivered
MSG_STATE_DEFERRED = "deferred" # msg delivered to a server but deferred for later delivery

# if user doesnt type for this time, we consider it idling
TYPING_IDLE_TIMEOUT = 5

# if user is typing, is-composing notificaitons will be sent in the following interval
TYPING_NOTIFY_INTERVAL = 30


_url_pattern = re.compile("((?:http://|https://|sip:|sips:)[^ )<>\r\n]+)")
_url_pattern_exact = re.compile("^((?:http://|https://|sip:|sips:)[^ )<>\r\n]+)$")


class ChatMessageObject(object):
    def __init__(self, msgid, text, is_html):
        self.msgid = msgid
        self.text = text
        self.is_html = is_html

def processHTMLText(text, usesmileys=True, is_html=False):
    def suball(pat, repl, html):
        ohtml = ""
        while ohtml != html:
            html = pat.sub(repl, html)
            ohtml = html
        return html

    if is_html:
        text = text.replace('\n', '')
    result = []
    tokens = _url_pattern.split(text)
    for token in tokens:
        if _url_pattern_exact.match(token):
            type, d, rest = token.partition(":")
            url = type + d + urllib.quote(rest, "/?&=;:,@+$#")
            token = r'<a href=\"%s\">%s</a>' % (url, escape_html(token))
        else:
            if not is_html:
                token = escape_html(token)
            else:
                token = token.replace('"', r'\"')
            if usesmileys:
                token = SmileyManager().subst_smileys_html(token)
        result.append(token)
    return "".join(result)


class ChatInputTextView(NSTextView):
    owner = None
    maxLength = None

    def initWithRect_(self, rect):
        self = NSTextView.initWithRect_(self, rect)
        if self:
            pass
        return self
    
    def setOwner(self, owner):
        self.owner = owner
    
    def setMaxLength_(self, l):
        self.maxLength = l
    
    def insertText_(self, text):
        if self.maxLength:
            oldText = self.textStorage().copy()
        NSTextView.insertText_(self, text)
        if self.maxLength and self.textStorage().length() > self.maxLength:
            self.textStorage().setAttributedString_(oldText)
            self.didChangeText()

    def readSelectionFromPasteboard_type_(self, pboard, type):
        if self.maxLength:
            text = pboard.stringForType_(type)
            if text:
                if self.textStorage().length() - self.rangeForUserTextChange().length + len(text) > self.maxLength:
                    text = text.substringWithRange_(NSMakeRange(0, self.maxLength - (self.textStorage().length() - self.rangeForUserTextChange().length)))
                self.textStorage().replaceCharactersInRange_withString_(self.rangeForUserTextChange(), text)
                self.didChangeText()
                return True
            return False
        else:
            return NSTextView.readSelectionFromPasteboard_type_(self, pboard, type)

    def preferredPasteboardTypeFromArray_restrictedToTypesFromArray_(self, availableTypes, allowedTypes):
        if availableTypes.containsObject_(NSStringPboardType) and (not allowedTypes or allowedTypes.containsObject_(NSStringPboardType)):
            return NSStringPboardType
        if hasattr(self.owner.delegate, "sendFiles") and availableTypes.containsObject_(NSFilenamesPboardType) and (not allowedTypes or allowedTypes.containsObject_(NSFilenamesPboardType)):
            return NSFilenamesPboardType
        return None

    def draggingEntered_(self, sender):
        if hasattr(self.owner.delegate, "sendFiles"):
            pboard = sender.draggingPasteboard()
            if pboard.types().containsObject_(NSFilenamesPboardType):
                return NSDragOperationAll
        return NSDragOperationNone

    def performDragOperation_(self, sender):
        if hasattr(self.owner.delegate, "sendFiles"):
            pboard = sender.draggingPasteboard()
            if pboard.types().containsObject_(NSFilenamesPboardType):
                ws = NSWorkspace.sharedWorkspace()
                fnames = pboard.propertyListForType_(NSFilenamesPboardType)
                return self.owner.delegate.sendFiles(fnames)
        return False

    def keyDown_(self, event):
        if event.keyCode() == 36 and (event.modifierFlags() & NSShiftKeyMask):
            self.insertText_('\r\n')
        else:
            super(ChatInputTextView, self).keyDown_(event)


class ChatWebView(WebView):
    def draggingEntered_(self, sender):
        if hasattr(self.frameLoadDelegate().delegate, "sendFiles"):
            pboard = sender.draggingPasteboard()
            if pboard.types().containsObject_(NSFilenamesPboardType):
                return NSDragOperationAll
        return NSDragOperationNone

    def performDragOperation_(self, sender):
        if hasattr(self.frameLoadDelegate().delegate, "sendFiles"):
            pboard = sender.draggingPasteboard()
            if pboard.types().containsObject_(NSFilenamesPboardType):
                ws = NSWorkspace.sharedWorkspace()
                fnames = pboard.propertyListForType_(NSFilenamesPboardType)
                return self.frameLoadDelegate().delegate.sendFiles(fnames)
        return False


class ChatViewController(NSObject):
    view = objc.IBOutlet()
    outputView = objc.IBOutlet()
    inputText = objc.IBOutlet()

    delegate = objc.IBOutlet()
    account = None
    rendered_messages = None
    finishedLoading = False

    expandSmileys = True

    rendered_messages = set()

    lastTypedTime = None
    lastTypeNotifyTime = None
    # timer is triggered every TYPING_IDLE_TIMEOUT, and a new is-composing msg is sent
    typingTimer = None

    def resetRenderedMessages(self):
        self.rendered_messages=set()

    def setAccount_(self, account):
        self.account = account

    def dealloc(self):
        if self.typingTimer:
            self.typingTimer.invalidate()
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        super(ChatViewController, self).dealloc()

    def awakeFromNib(self):
        self.outputView.registerForDraggedTypes_(NSArray.arrayWithObject_(NSFilenamesPboardType))
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "webviewFinishedLoading:", WebViewProgressFinishedNotification, self.outputView)
        if self.inputText:
            self.inputText.registerForDraggedTypes_(NSArray.arrayWithObject_(NSFilenamesPboardType))
            self.inputText.setOwner(self)
            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "textDidChange:", NSTextDidChangeNotification, self.inputText)
        self.messageQueue = []

    def setContentFile_(self, path):
        self.finishedLoading = False
        request = NSURLRequest.alloc().initWithURL_(NSURL.alloc().initFileURLWithPath_(path))
        self.outputView.mainFrame().loadRequest_(request)
        assert self.outputView.preferences().isJavaScriptEnabled()

    def close(self):
        self.inputText.setOwner(None)
        self.outputView.close()

    def appendAttributedString_(self, text):
        storage = self.inputText.textStorage()
        storage.beginEditing()
        storage.appendAttributedString_(text)
        storage.endEditing()

    def textDidChange_(self, notification):
        self.lastTypedTime = datetime.datetime.now()
        if self.inputText.textStorage().length() == 0:
            self.resetTyping()
        else:
            if not self.lastTypeNotifyTime or time.time() - self.lastTypeNotifyTime > TYPING_NOTIFY_INTERVAL:
                self.lastTypeNotifyTime = time.time()
                self.delegate.chatView_becameActive_(self, self.lastTypedTime)
            if self.typingTimer:
                # delay the timeout a bit more
                self.typingTimer.setFireDate_(NSDate.dateWithTimeIntervalSinceNow_(TYPING_IDLE_TIMEOUT))
            else:
                self.typingTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(TYPING_IDLE_TIMEOUT, self, "becameIdle:", None, False)

    def resetTyping(self):
        self.becameIdle_(None)

    def becameIdle_(self, timer):
        if self.typingTimer:
            self.typingTimer.invalidate()
        # if we got here, it means there was no typing activity in the last TYPING_IDLE_TIMEOUT seconds
        # so change state back to idle
        self.typingTimer = None
        self.delegate.chatView_becameIdle_(self, self.lastTypedTime)
        self.lastTypeNotifyTime = None

    def markMessage(self, msgid, state, private=False): # delegate
        if state == MSG_STATE_DELIVERED:
            is_private = 1 if private else "null"
            script = "markDelivered('%s',%s)"%(msgid, is_private)
            call_in_gui_thread(self.outputView.stringByEvaluatingJavaScriptFromString_, script)
        elif state == MSG_STATE_DEFERRED:
            script = "markDeferred('%s')"%msgid
            call_in_gui_thread(self.outputView.stringByEvaluatingJavaScriptFromString_, script)
        elif state == MSG_STATE_FAILED:
            script = "markFailed('%s')"%msgid
            call_in_gui_thread(self.outputView.stringByEvaluatingJavaScriptFromString_, script)

    def clear(self):
        if self.finishedLoading:
            self.outputView.stringByEvaluatingJavaScriptFromString_("clear()")
        else:
            self.messageQueue = []

    def showSystemMessage(self, text, timestamp=None, is_error=False):
        if timestamp is None:
            timestamp = datetime.datetime.now(tzlocal())
        if type(timestamp) is datetime.datetime:
            if timestamp.date() != datetime.date.today():
                timestamp = time.strftime("%F %T", time.localtime(calendar.timegm(timestamp.utctimetuple())))
            else:
                timestamp = time.strftime("%T", time.localtime(calendar.timegm(timestamp.utctimetuple())))

        is_error = 1 if is_error else "null"
        script = """renderSystemMessage("%s", "%s", %s)""" % (processHTMLText(text), timestamp, is_error)

        if self.finishedLoading:
            self.outputView.stringByEvaluatingJavaScriptFromString_(script)
        else:
            self.messageQueue.append(script)

    def showMessage(self, msgid, direction, sender, icon_path, text, timestamp, is_html=False, state='', recipient='', is_private=False, history_entry=False):
        # keep track of rendered messages to toggle the smileys
        rendered_message = ChatMessageObject(msgid, text, is_html)
        self.rendered_messages.add(rendered_message)

        if timestamp.date() != datetime.date.today():
            displayed_timestamp = time.strftime("%F %T", time.localtime(calendar.timegm(timestamp.utctimetuple())))
        else:
            displayed_timestamp = time.strftime("%T", time.localtime(calendar.timegm(timestamp.utctimetuple())))

        text = processHTMLText(text, self.expandSmileys, is_html)
        private = 1 if is_private else "null"

        if is_private and sender and recipient:
            sender_uri = format_identity_from_text(sender)[0]
            label = 'Private message to %s' % cgi.escape(recipient) if direction == 'outgoing' else 'Private message from %s' % cgi.escape(sender)
        else: 
            label = cgi.escape(format_identity(self.account)) if sender is None else cgi.escape(sender)

        script = """renderMessage('%s', '%s', '%s', '%s', "%s", '%s', '%s', %s)""" % (msgid, direction, label, icon_path, text, displayed_timestamp, state, private)

        if self.finishedLoading:
            self.outputView.stringByEvaluatingJavaScriptFromString_(script)
        else:
            self.messageQueue.append(script)

        if hasattr(self.delegate, "chatViewDidGetNewMessage_"):
            self.delegate.chatViewDidGetNewMessage_(self)

    def toggleSmileys(self, expandSmileys):
        for entry in self.rendered_messages:
            self.updateMessage(entry.msgid, entry.text, entry.is_html, expandSmileys)

    def updateMessage(self, msgid, text, is_html, expandSmileys):
        text = processHTMLText(text, expandSmileys, is_html)
        script = """updateMessageBodyContent('%s', "%s")""" % (msgid, text)
        call_in_gui_thread(self.outputView.stringByEvaluatingJavaScriptFromString_, script)
        self.outputView.stringByEvaluatingJavaScriptFromString_(script)

    def webviewFinishedLoading_(self, notification):
        self.document = self.outputView.mainFrameDocument() 
        self.finishedLoading = True
        if hasattr(self.delegate, "chatViewDidLoad_"):
            self.delegate.chatViewDidLoad_(self)
        for script in self.messageQueue:
            self.outputView.stringByEvaluatingJavaScriptFromString_(script)
        self.messageQueue = []

    def webView_contextMenuItemsForElement_defaultMenuItems_(self, sender, element, defaultItems):
        return None
        
    def webView_decidePolicyForNavigationAction_request_frame_decisionListener_(self, webView, info, request, frame, listener):
        # intercept link clicks so that they are opened in Safari
        theURL = info[WebActionOriginalURLKey]
        if theURL.scheme() == "file":
            listener.use()
        else:
            listener.ignore()
            NSWorkspace.sharedWorkspace().openURL_(theURL)


