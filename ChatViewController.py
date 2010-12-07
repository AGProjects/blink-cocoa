# Copyright (C) 2009 AG Projects. See LICENSE for details.     
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
from util import call_in_gui_thread, escape_html, format_identity


MSG_STATE_SENDING = "sending" # middleware told us the message is being sent
MSG_STATE_FAILED = "failed" # msg delivery failed
MSG_STATE_DELIVERED = "delivered" # msg successfully delivered
MSG_STATE_DEFERRED = "deferred" # msg deferred for later delivery

# if user doesnt type for this time, we consider it idling
TYPING_IDLE_TIMEOUT = 5

# if user is typing, is-composing notificaitons will be sent in the following interval
TYPING_NOTIFY_INTERVAL = 30


_url_pattern = re.compile("((?:http://|https://|sip:|sips:)[^ )<>\r\n]+)")
_url_pattern_exact = re.compile("^((?:http://|https://|sip:|sips:)[^ )<>\r\n]+)$")


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
    history = None
    account = None
    finishedLoading = False

    expandSmileys = True

    lastTypedTime = None
    lastTypeNotifyTime = None
    # timer is triggered every TYPING_IDLE_TIMEOUT, and a new is-composing msg is sent
    typingTimer = None

    def setAccount_(self, account):
        self.account = account

    def setHistory_(self, history):
        self.history = history

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

    def updateMessageId(self, oldid, newid): # delegate
        script = "fixupMessageId('%s', '%s')"%(oldid, newid)
        self.outputView.stringByEvaluatingJavaScriptFromString_(script)
        if self.history:
            self.history.set_sent(oldid, newid)

    def markMessage(self, msgid, state): # delegate
        if state == MSG_STATE_DELIVERED:
            script = "markDelivered('%s')"%msgid
            call_in_gui_thread(self.outputView.stringByEvaluatingJavaScriptFromString_, script)
            if self.history:
                self.history.set_delivered(msgid)
        elif state == MSG_STATE_DEFERRED:
            script = "markDeferred('%s')"%msgid
            call_in_gui_thread(self.outputView.stringByEvaluatingJavaScriptFromString_, script)
            if self.history:
                self.history.set_deferred(msgid)
        elif state == MSG_STATE_FAILED:
            script = "markFailed('%s')"%msgid
            call_in_gui_thread(self.outputView.stringByEvaluatingJavaScriptFromString_, script)
            if self.history:
                self.history.set_failed(msgid)

    def clear(self):
        if self.finishedLoading:
            self.outputView.stringByEvaluatingJavaScriptFromString_("clear()")
        else:
            self.messageQueue = []

    def showMessage(self, msgid, sender, icon_path, text, timestamp, is_html=False, history_entry=False, state=None): # delegate
        astate = state = state or ''
        if self.history:
            incoming = sender is not None
            if sender is not None:
                state = ""
            else:
                if msgid and msgid.startswith("-"):
                    state = "queued"
                else:
                    state = "sent"
            log_timestamp = timestamp.strftime("%F %T")
            self.history.log(
                    id=msgid,
                    direction=incoming and "receive" or "send",
                    sender=sender or format_identity(self.account),
                    text=text,
                    send_time=incoming and None or log_timestamp,
                    delivered_time=incoming and log_timestamp or None,
                    state=state,
                    type=is_html and "html" or "text")

        if timestamp.date() != datetime.date.today():
            displayed_timestamp = time.strftime("%F %T", time.localtime(calendar.timegm(timestamp.utctimetuple())))
        else:
            displayed_timestamp = time.strftime("%T", time.localtime(calendar.timegm(timestamp.utctimetuple())))

        text = processHTMLText(text, self.expandSmileys, is_html)
        if sender is None:
            name = cgi.escape(format_identity(self.account))
            script = """addChatMessage('%s', '%s', '%s', "%s", '%s', '%s')""" % (msgid, name, icon_path, text, displayed_timestamp, astate)
        else:
            name = cgi.escape(sender)
            script = """addChatMessage(null, '%s', '%s', "%s", '%s', '%s')""" % (name, icon_path, text, displayed_timestamp, astate)
        if self.finishedLoading:
            self.outputView.stringByEvaluatingJavaScriptFromString_(script)
        else:
            self.messageQueue.append(script)
        if hasattr(self.delegate, "chatViewDidGetNewMessage_"):
            self.delegate.chatViewDidGetNewMessage_(self)
        NotificationCenter().post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=TimestampedNotificationData(message=text, direction='outgoing' if sender is None else 'incoming', history_entry=history_entry))

    def writeSysMessage(self, text, timestamp=None):
        if timestamp is None:
            timestamp = datetime.datetime.utcnow()
        if type(timestamp) is datetime.datetime:
            if timestamp.date() != datetime.date.today():
                timestamp = time.strftime("%F %T", time.localtime(calendar.timegm(timestamp.utctimetuple())))
            else:
                timestamp = time.strftime("%T", time.localtime(calendar.timegm(timestamp.utctimetuple())))
        script = """addSysMessage("%s", "%s")""" % (processHTMLText(text), timestamp)
        if self.finishedLoading:
            self.outputView.stringByEvaluatingJavaScriptFromString_(script)
        else:
            self.messageQueue.append(script)

    def writeOldMessage(self, msgid, sender, icon_path, text, timestamp, state, is_html):
        if type(timestamp) is datetime.datetime:
            # logs are in localtime
            if timestamp.date() != datetime.date.today():
                timestamp = time.strftime("%F %T", timestamp.utctimetuple())
            else:
                timestamp = time.strftime("%T", timestamp.utctimetuple())
        text = processHTMLText(text, self.expandSmileys, is_html=is_html)
        if sender is None:
            name = cgi.escape(format_identity(self.account))
        else:
            name = cgi.escape(sender)
        if msgid:
            msgid = "'%s'"%msgid
        else:
            msgid = "null"
        me = format_identity(self.account)
        script = """addOldChatMessage(%i, %s, '%s', "%s", "%s", "%s", '%s')""" % (int(name==me), msgid, name, icon_path, text, timestamp, state)
        if self.finishedLoading:
            self.outputView.stringByEvaluatingJavaScriptFromString_(script)
        else:
            self.messageQueue.append(script)

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


