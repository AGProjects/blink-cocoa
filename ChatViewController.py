# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

__all__ = ['ChatInputTextView', 'ChatViewController', 'processHTMLText',
           'MSG_STATE_SENDING', 'MSG_STATE_FAILED', 'MSG_STATE_DELIVERED', 'MSG_STATE_DEFERRED']

import calendar
import cgi
import datetime
import objc
import os
import re
import time
import urllib
import uuid

from AppKit import NSCommandKeyMask, NSDragOperationNone, NSDragOperationCopy, NSFilenamesPboardType, NSShiftKeyMask, NSTextDidChangeNotification, NSOnState
from Foundation import NSArray, NSDate, NSLocalizedString, NSMakeRange, NSNotificationCenter, NSObject, NSTextView, NSTimer, NSURL, NSURLRequest, NSWorkspace
from WebKit import WebView, WebViewProgressFinishedNotification, WebActionOriginalURLKey

from application.notification import NotificationCenter
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.util import ISOTimestamp
from resources import Resources
from SmileyManager import SmileyManager
from util import escape_html


MSG_STATE_SENDING = "sending" # middleware told us the message is being sent
MSG_STATE_FAILED = "failed" # msg delivery failed
MSG_STATE_DELIVERED = "delivered" # msg successfully delivered
MSG_STATE_DEFERRED = "deferred" # msg delivered to a server but deferred for later delivery

# if user doesnt type for this time, we consider it idling
TYPING_IDLE_TIMEOUT = 5

# if user is typing, is-composing notifications will be sent in the following interval
TYPING_NOTIFY_INTERVAL = 30


_url_pattern = re.compile("((?:http://|https://|sip:|sips:)[^ )<>\r\n]+)")
_url_pattern_exact = re.compile("^((?:http://|https://|sip:|sips:)[^ )<>\r\n]+)$")


class ChatMessageObject(object):
    def __init__(self, call_id, msgid, content, is_html, timestamp=None, media_type='chat'):
        self.call_id = call_id
        self.msgid = msgid
        self.content = content
        self.is_html = is_html
        self.timestamp = timestamp
        self.media_type = media_type


def processHTMLText(content='', usesmileys=True, is_html=False):
    if is_html:
        content = urlify(content)
        content = content.replace('\n', '')
        content = content.replace('\\', '&#92;')

    result = []
    tokens = _url_pattern.split(content)
    for token in tokens:
        if not is_html and _url_pattern_exact.match(token):
            type, d, rest = token.partition(":")
            url = type + d + urllib.quote(rest.encode('utf-8'), "/%?&=;:,@+$#")
            token = r'<a href=\"%s\">%s</a>' % (url, escape_html(token))
        else:
            if not is_html:
                token = escape_html(token)
            else:
                token = token.replace('"', r'\"')
            if usesmileys:
                token = SmileyManager().subst_smileys_html(token)
        result.append(token)
        content = "".join(result)

    return content


class ChatInputTextView(NSTextView):
    owner = None
    maxLength = None

    def dealloc(self):
        objc.super(ChatInputTextView, self).dealloc()

    def initWithRect_(self, rect):
        self = NSTextView.initWithRect_(self, rect)
        if self:
            pass
        return self

    def setOwner(self, owner):
        self.owner = owner   # ChatViewController

    def setMaxLength_(self, l):
        self.maxLength = l

    def insertText_(self, content):
        if self.maxLength:
            oldText = self.textStorage().copy()
        NSTextView.insertText_(self, content)
        if self.maxLength and self.textStorage().length() > self.maxLength:
            self.textStorage().setAttributedString_(oldText)
            self.didChangeText()

    def readSelectionFromPasteboard_type_(self, pboard, type):
        self.owner.textWasPasted = True
        if self.maxLength:
            content = pboard.stringForType_(type)
            if content:
                if self.textStorage().length() - self.rangeForUserTextChange().length + len(content) > self.maxLength:
                    content = content.substringWithRange_(NSMakeRange(0, self.maxLength - (self.textStorage().length() - self.rangeForUserTextChange().length)))
                self.textStorage().replaceCharactersInRange_withString_(self.rangeForUserTextChange(), content)
                self.didChangeText()
                return True
            return False
        else:
            return NSTextView.readSelectionFromPasteboard_type_(self, pboard, type)

    def draggingEntered_(self, sender):
        pboard = sender.draggingPasteboard()
        if pboard.types().containsObject_(NSFilenamesPboardType) and hasattr(self.owner.delegate, "sendFiles"):
            pboard = sender.draggingPasteboard()
            fnames = pboard.propertyListForType_(NSFilenamesPboardType)
            for f in fnames:
                if not os.path.isfile(f) and not os.path.isdir(f):
                    return NSDragOperationNone
            return NSDragOperationCopy
        return NSDragOperationNone

    def prepareForDragOperation_(self, sender):
        pboard = sender.draggingPasteboard()
        if pboard.types().containsObject_(NSFilenamesPboardType):
            fnames = pboard.propertyListForType_(NSFilenamesPboardType)
            for f in fnames:
                if not os.path.isfile(f) and not os.path.isdir(f):
                    return False
            return True
        return False

    def performDragOperation_(self, sender):
        pboard = sender.draggingPasteboard()
        if hasattr(self.owner.delegate, "sendFiles") and pboard.types().containsObject_(NSFilenamesPboardType):
            filenames = pboard.propertyListForType_(NSFilenamesPboardType)
            return self.owner.delegate.sendFiles(filenames)
        return False

    def keyDown_(self, event):
        if event.keyCode() == 36 and (event.modifierFlags() & NSShiftKeyMask):
            self.insertText_('\r\n')
        elif (event.modifierFlags() & NSCommandKeyMask):
            keys = event.characters()
            if keys[0] == 'i' and self.owner.delegate.sessionController.info_panel is not None:
                self.owner.delegate.sessionController.info_panel.toggle()
        else:
            objc.super(ChatInputTextView, self).keyDown_(event)


class ChatWebView(WebView):
    def dealloc(self):
        objc.super(ChatWebView, self).dealloc()

    def draggingEntered_(self, sender):
        pboard = sender.draggingPasteboard()
        if pboard.types().containsObject_(NSFilenamesPboardType) and hasattr(self.frameLoadDelegate().delegate, "sendFiles"):
            fnames = pboard.propertyListForType_(NSFilenamesPboardType)
            for f in fnames:
                if not os.path.isfile(f) and not os.path.isdir(f):
                    return NSDragOperationNone
            return NSDragOperationCopy
        return NSDragOperationNone

    def performDragOperation_(self, sender):
        if hasattr(self.frameLoadDelegate().delegate, "sendFiles"):
            pboard = sender.draggingPasteboard()
            if pboard.types().containsObject_(NSFilenamesPboardType):
                filenames = pboard.propertyListForType_(NSFilenamesPboardType)
                return self.frameLoadDelegate().delegate.sendFiles(filenames)
        return False


class ChatViewController(NSObject):
    view = objc.IBOutlet()
    outputView = objc.IBOutlet()
    inputText = objc.IBOutlet()
    inputView = objc.IBOutlet()
    lastMessagesLabel = objc.IBOutlet()
    loadingProgressIndicator = objc.IBOutlet()
    loadingTextIndicator = objc.IBOutlet()
    searchMessagesBox = objc.IBOutlet()
    showRelatedMessagesButton = objc.IBOutlet()
    encryptionDisabledWarningLabel = objc.IBOutlet()
    continueWithoutEncryptionCheckbox = objc.IBOutlet()

    splitterHeight = None

    delegate = objc.IBOutlet() # ChatController
    account = None
    rendered_messages = set()
    finishedLoading = False
    search_text = None
    related_messages = []
    show_related_messages = False

    expandSmileys = True
    editorVisible = False

    rendered_messages = set()
    pending_messages = {}

    video_source = None
    video_visible = False
    video_initialized = False

    lastTypedTime = None
    lastTypeNotifyTime = None
    # timer is triggered every TYPING_IDLE_TIMEOUT, and a new is-composing msg is sent
    typingTimer = None

    scrollingTimer = None

    handle_scrolling = True
    scrolling_zoom_factor = 0

    editorIsComposing = False
    scrolling_back = False

    last_sender = None
    previous_msgid = ""

    textWasPasted = False


    @property
    def sessionController(self):
        return self.delegate.sessionController

    def resetRenderedMessages(self):
        self.rendered_messages=[]

    def setAccount_(self, account):
        self.account = account

    def awakeFromNib(self):
        self.outputView.setShouldCloseWithWindow_(True)
        self.outputView.registerForDraggedTypes_(NSArray.arrayWithObject_(NSFilenamesPboardType))
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "webviewFinishedLoading:", WebViewProgressFinishedNotification, self.outputView)
        if self.inputText:
            self.inputText.registerForDraggedTypes_(NSArray.arrayWithObject_(NSFilenamesPboardType))
            self.inputText.setOwner(self)
            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "textDidChange:", NSTextDidChangeNotification, self.inputText)

        self.messageQueue = []

    @objc.IBAction
    def showRelatedMessages_(self, sender):
        self.show_related_messages = True
        self.searchMessages_(None)
        self.showRelatedMessagesButton.setHidden_(True)

    @objc.IBAction
    def searchMessages_(self, sender):
        for message in self.rendered_messages:
            self.unmarkFound(message.msgid)

        self.search_text = unicode(self.searchMessagesBox.stringValue()).strip() or None

        call_ids = set()
        if self.search_text is not None:
            for message in self.rendered_messages:
                if self.search_text.lower() in message.content.lower():
                    if message.call_id:
                        call_ids.add(message.call_id)
                        if message.media_type == 'sms':
                            pivot_timestamp = message.timestamp
                            try:
                                pivot_index = self.rendered_messages.index(message)
                            except ValueError:
                                pass
                            else:
                                index = pivot_index
                                while True:
                                    index -= 1
                                    if index <= 0:
                                        break

                                    try:
                                        previous_message = self.rendered_messages[index]
                                    except IndexError:
                                        break

                                    if previous_message.media_type != 'sms':
                                        break

                                    timediff = pivot_timestamp - previous_message.timestamp
                                    if timediff.seconds < 3600:
                                        call_ids.add(previous_message.call_id)
                                    else:
                                        break

                                index = pivot_index
                                while True:
                                    index += 1

                                    try:
                                        next_message = self.rendered_messages[index]
                                    except IndexError:
                                        break

                                    if next_message.media_type != 'sms':
                                        break

                                    timediff = next_message.timestamp - pivot_timestamp

                                    if timediff.seconds < 3600:
                                        call_ids.add(next_message.call_id)
                                    else:
                                        break

                    self.htmlBoxVisible('c%s' % message.msgid)
                    self.markFound(message.msgid)
                    call_ids.discard(message.msgid)
                else:
                    self.htmlBoxHidden('c%s' % message.msgid)
        else:
            for message in self.rendered_messages:
                self.htmlBoxVisible('c%s' % message.msgid)

        self.related_messages = [message for message in self.rendered_messages if message.call_id in call_ids]

        if self.show_related_messages:
            for message in self.related_messages:
                self.htmlBoxVisible('c%s' % message.msgid)
            self.show_related_messages = False

        if self.related_messages:
            self.showRelatedMessagesButton.setHidden_(False)

    def htmlBoxVisible(self, msgid):
        script = """htmlBoxVisible('%s')""" % msgid
        self.executeJavaScript(script)

    def htmlBoxHidden(self, msgid):
        script = """htmlBoxHidden('%s')""" % msgid
        self.executeJavaScript(script)

    def markFound(self, msgid):
        script = """markFound('%s')""" % msgid
        self.executeJavaScript(script)

    def unmarkFound(self, msgid):
        script = """unmarkFound('%s')""" % msgid
        self.executeJavaScript(script)

    def setHandleScrolling_(self, scrolling):
        self.handle_scrolling = scrolling

    def setContentFile_(self, path):
        self.finishedLoading = False
        request = NSURLRequest.alloc().initWithURL_(NSURL.alloc().initFileURLWithPath_(path))
        self.outputView.mainFrame().loadRequest_(request)
        assert self.outputView.preferences().isJavaScriptEnabled()

    def appendAttributedString_(self, content):
        storage = self.inputText.textStorage()
        storage.beginEditing()
        storage.appendAttributedString_(content)
        storage.endEditing()

    @objc.IBAction
    def confirmWithoutEncryption_(self, sender):
        self.continueWithoutEncryptionCheckbox.setHidden_(True)
        self.encryptionDisabledWarningLabel.setHidden_(True)

    def textDidChange_(self, notification):
        self.lastTypedTime = datetime.datetime.now()
        if self.inputText.textStorage().length() == 0:
            self.becameIdle_(None)
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
        if self.typingTimer:
            self.typingTimer.invalidate()
        self.typingTimer = None
        self.lastTypeNotifyTime = None

    def becameIdle_(self, timer):
        # if we got here, it means there was no typing activity in the last TYPING_IDLE_TIMEOUT seconds
        # so change state back to idle
        lastTypedTime = self.lastTypedTime
        self.resetTyping()
        self.delegate.chatView_becameIdle_(self, lastTypedTime)

    def updateEncryptionLock(self, msgid, encryption=None):
        if encryption is None:
            return

        if encryption == '':
            lock_icon_path = Resources.get('unlocked-darkgray.png')
        else:
            lock_icon_path = Resources.get('locked-green.png' if encryption == 'verified' else 'locked-red.png')
        script = "updateEncryptionLock('%s','%s')" % (msgid, lock_icon_path)
        self.executeJavaScript(script)

    def markMessage(self, msgid, state, private=False): # delegate
        if state == MSG_STATE_DELIVERED:
            is_private = 1 if private else "null"
            script = "markDelivered('%s',%s)"%(msgid, is_private)
            self.executeJavaScript(script)
        elif state == MSG_STATE_DEFERRED:
            script = "markDeferred('%s')"%msgid
            self.executeJavaScript(script)
        elif state == MSG_STATE_FAILED:
            script = "markFailed('%s')"%msgid
            self.executeJavaScript(script)

    def clear(self):
        if self.finishedLoading:
            self.executeJavaScript("clear()")
        else:
            self.messageQueue = []

    def showSystemMessage(self, call_id, content, timestamp=None, is_error=False):
        msgid = str(uuid.uuid1())
        rendered_message = ChatMessageObject(call_id, msgid, content, False, timestamp)
        self.rendered_messages.append(rendered_message)

        if timestamp is None:
            timestamp = ISOTimestamp.now()
        if isinstance(timestamp, datetime.datetime):
            if timestamp.date() != datetime.date.today():
                timestamp = time.strftime("%F %H:%M", time.localtime(calendar.timegm(timestamp.utctimetuple())))
            else:
                timestamp = time.strftime("%H:%M", time.localtime(calendar.timegm(timestamp.utctimetuple())))

        is_error = 1 if is_error else "null"
        script = """renderSystemMessage('%s', "%s", "%s", %s)""" % (msgid, processHTMLText(content), timestamp, is_error)

        if self.finishedLoading:
            self.executeJavaScript(script)
        else:
            self.messageQueue.append(script)

    def showMessage(self, call_id, msgid, direction, sender, icon_path, content, timestamp, is_html=False, state='', recipient='', is_private=False, history_entry=False, media_type='chat', encryption=None):
        lock_icon_path = Resources.get('unlocked-darkgray.png')
        if encryption is not None:
            if encryption == '':
                lock_icon_path = Resources.get('unlocked-darkgray.png')
            else:
                lock_icon_path = Resources.get('locked-green.png' if encryption == 'verified' else 'locked-red.png')

        if self.last_sender == sender:
            icon_path = "null"
        else:
            icon_path = "'%s'" % icon_path

        self.last_sender = sender

        if not history_entry and not self.delegate.isOutputFrameVisible():
            self.delegate.showChatViewWhileVideoActive()

        # keep track of rendered messages to toggle the smileys or search their content later
        rendered_message = ChatMessageObject(call_id, msgid, content, is_html, timestamp, media_type)
        self.rendered_messages.append(rendered_message)

        if timestamp.date() != datetime.date.today():
            displayed_timestamp = time.strftime("%F %H:%M", time.localtime(calendar.timegm(timestamp.utctimetuple())))
        else:
            displayed_timestamp = time.strftime("%H:%M", time.localtime(calendar.timegm(timestamp.utctimetuple())))

        content = processHTMLText(content, self.expandSmileys, is_html)
        private = 1 if is_private else "null"

        if is_private and recipient:
            label = NSLocalizedString("Private message to %s", "Label") % cgi.escape(recipient) if direction == 'outgoing' else NSLocalizedString("Private message from %s", "Label") % cgi.escape(sender)
        else:
            if hasattr(self.delegate, "sessionController"):
                label = cgi.escape(self.delegate.sessionController.nickname or self.account.display_name or self.account.id) if sender is None else cgi.escape(sender)
            else:
                label = cgi.escape(self.account.display_name or self.account.id) if sender is None else cgi.escape(sender)

        try:
            script = """renderMessage('%s', '%s', '%s', %s, "%s", '%s', '%s', %s, '%s', '%s')""" % (msgid, direction, label, icon_path, content, displayed_timestamp, state, private, lock_icon_path, self.previous_msgid)
        except UnicodeDecodeError:
            script = """renderMessage('%s', '%s', '%s', %s, "%s", '%s', '%s', %s, '%s', '%s')""" % (msgid, direction, label, icon_path, content.decode('utf-8'), displayed_timestamp, state, private, lock_icon_path, self.previous_msgid)
        except:
            self.delegate.showSystemMessage("Chat message id %s rendering error: %s" % (msgid, e), ISOTimestamp.now(), True)
            return

        if self.finishedLoading:
            self.executeJavaScript(script)
        else:
            self.messageQueue.append(script)

        if hasattr(self.delegate, "chatViewDidGetNewMessage_"):
            self.delegate.chatViewDidGetNewMessage_(self)

        self.previous_msgid = msgid

    def toggleSmileys(self, expandSmileys):
        for entry in self.rendered_messages:
            self.updateMessage(entry.msgid, entry.content, entry.is_html, expandSmileys)

    def updateMessage(self, msgid, content, is_html, expandSmileys):
        content = processHTMLText(content, expandSmileys, is_html)
        script = """updateMessageBodyContent('%s', "%s")""" % (msgid, content)
        self.executeJavaScript(script)

    def toggleCollaborationEditor(self):
        if self.editorVisible:
            self.hideCollaborationEditor()
        else:
            self.showCollaborationEditor()

    def showCollaborationEditor(self):
        self.editorVisible = True
        self.last_scrolling_label = self.lastMessagesLabel.stringValue()
        self.lastMessagesLabel.setStringValue_(NSLocalizedString("Click on Editor toolbar button to switch back to the chat session", "Label"))
        self.searchMessagesBox.setHidden_(True)
        self.showRelatedMessagesButton.setHidden_(True)
        settings = SIPSimpleSettings()

        frame=self.inputView.frame()
        self.splitterHeight = frame.size.height
        frame.size.height = 0
        self.inputView.setFrame_(frame)

        script = """showCollaborationEditor("%s", "%s")""" % (self.delegate.sessionController.collaboration_form_id, settings.server.collaboration_url)
        self.executeJavaScript(script)

    def hideCollaborationEditor(self):
        self.editorVisible = False
        self.lastMessagesLabel.setStringValue_(self.last_scrolling_label)
        self.searchMessagesBox.setHidden_(False)
        if self.related_messages:
            self.showRelatedMessagesButton.setHidden_(False)

        if self.splitterHeight is not None:
            frame=self.inputView.frame()
            frame.size.height = self.splitterHeight
            self.inputView.setFrame_(frame)

        script = "hideCollaborationEditor()"
        self.executeJavaScript(script)

    def scrollToBottom(self):
        script = "scrollToBottom()"
        self.executeJavaScript(script)

    def scrollToId(self, id):
        script = """scrollToId("%s")""" % id
        self.executeJavaScript(script)

    def executeJavaScript(self, script):
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
        for item in defaultItems:
            if item.title() == 'Reload':
                del defaultItems[defaultItems.index(item)]
                break
        return defaultItems

    def webView_decidePolicyForNavigationAction_request_frame_decisionListener_(self, webView, info, request, frame, listener):
        # intercept when user clicks on links so that we process them in different ways
        theURL = info[WebActionOriginalURLKey]

        if self.delegate and hasattr(self.delegate, 'getWindow'):
            window = self.delegate.getWindow()
            if window and window.showConferenceSharedScreen(theURL.absoluteString()):
                return

        if theURL.scheme() == "file":
            listener.use()
        else:
            # use system wide web browser
            if theURL.absoluteString() in self.delegate.sessionController.screensharing_urls.values():
                self.delegate.chatWindowController.showConferenceSharedScreen(theURL.absoluteString())
            else:
                listener.ignore()
                NSWorkspace.sharedWorkspace().openURL_(theURL)


    # capture java-script functions
    def isSelectorExcludedFromWebScript_(self, sel):
        if sel == "collaborativeEditorisTyping":
            return False
        if sel == "isScrolling:":
            return False

        return True

    def isScrolling_(self, scrollTop):
        if not self.handle_scrolling:
            return

        if self.editorVisible:
            return

        if scrollTop < 0:
            if self.scrollingTimer is None:
                self.scrollingTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1, self, "scrollTimerDelay:", None, False)
            self.scrolling_back = True
        else:
            self.scrolling_back = False
            if self.scrollingTimer is not None:
                self.scrollingTimer.invalidate()
                self.scrollingTimer = None

            if scrollTop == 0 and self.handle_scrolling:
                current_label = self.lastMessagesLabel.stringValue()
                new_label = NSLocalizedString("Keep scrolling up for more than one second to load older messages", "Label")
                if current_label != new_label and NSLocalizedString("Loading", "Label") not in current_label:
                    self.lastMessagesLabel.setStringValue_(new_label)
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(4, self, "showLastScrollLabel:", None, False)

    def showLastScrollLabel_(self, timer):
        if self.delegate.zoom_period_label != '':
            self.lastMessagesLabel.setStringValue_(self.delegate.zoom_period_label)

    def scrollTimerDelay_(self, timer):
        if self.scrolling_back:
            self.scrolling_zoom_factor += 1
            if self.scrolling_zoom_factor > 7:
                self.scrolling_zoom_factor = 7
            self.loadingProgressIndicator.startAnimation_(None)
            self.lastMessagesLabel.setStringValue_(NSLocalizedString("Loading messages...", "Label"))
            if self.scrolling_zoom_factor == 1:
                zoom_period_label = NSLocalizedString("Loading messages from last day...", "Label")
            elif self.scrolling_zoom_factor == 2:
                zoom_period_label = NSLocalizedString("Loading messages from last week...", "Label")
            elif self.scrolling_zoom_factor == 3:
                zoom_period_label = NSLocalizedString("Loading messages from last month...", "Label")
            elif self.scrolling_zoom_factor == 4:
                zoom_period_label = NSLocalizedString("Loading messages from last three months...", "Label")
            elif self.scrolling_zoom_factor == 5:
                zoom_period_label = NSLocalizedString("Loading messages from last six months...", "Label")
            elif self.scrolling_zoom_factor == 6:
                zoom_period_label = NSLocalizedString("Loading messages from last year...", "Label")
            elif self.scrolling_zoom_factor == 7:
                zoom_period_label = NSLocalizedString("Loading all messages...", "Label")
            self.lastMessagesLabel.setStringValue_(zoom_period_label)
            self.delegate.scroll_back_in_time()

    def collaborativeEditorisTyping(self):
        self.editorIsComposing = True
        self.delegate.resetIsComposingTimer(5)

        NotificationCenter().post_notification("BlinkCollaborationEditorContentHasChanged", sender=self)

    def webView_didClearWindowObject_forFrame_(self, sender, windowObject, frame):
        windowObject.setValue_forKey_(self, "blink")

    def close(self):
        # memory clean up
        self.rendered_messages = set()
        self.pending_messages = {}
        self.view.removeFromSuperview()
        self.inputText.setOwner(None)
        self.inputText.removeFromSuperview()
        self.outputView.close()
        self.outputView.removeFromSuperview()
        self.release()

    def dealloc(self):
        if self.typingTimer:
            self.typingTimer.invalidate()
            self.typingTimer = None
        if self.scrollingTimer:
            self.scrollingTimer.invalidate()
            self.scrollingTimer = None
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        objc.super(ChatViewController, self).dealloc()


class Transform(object):
    """Abstraction for a regular expression transform.

        http://google-app-engine-samples.googlecode.com/svn/trunk/cccwiki/wiki.py

        Transform subclasses have two properties:
        regexp: the regular expression defining what will be replaced
        replace(MatchObject): returns a string replacement for a regexp match

        We iterate over all matches for that regular expression, calling replace()
        on the match to determine what text should replace the matched text.

        The Transform class is more expressive than regular expression replacement
        because the replace() method can execute arbitrary code to, e.g., look
        up a WikiWord to see if the page exists before determining if the WikiWord
        should be a link.
        """
    def run(self, content):
        """Runs this transform over the given content.

            Args:
            content: The string data to apply a transformation to.

            Returns:
            A new string that is the result of this transform.
            """
        parts = []
        offset = 0
        for match in self.regexp.finditer(content):
            parts.append(content[offset:match.start(0)])
            parts.append(self.replace(match))
            offset = match.end(0)
        parts.append(content[offset:])
        return ''.join(parts)


class AutoLink(Transform):
    """A transform that auto-links URLs."""
    def __init__(self):
        self.regexp = re.compile(r'([^"])\b((http|https)://[^ \t\n\r<>\(\)"]+' \
                                 r'[^ \t\n\r<>\(\)&"\.])')

    def replace(self, match):
        url = match.group(2)
        return match.group(1) + '<a href="%s">%s</a>' % (url, url)

def urlify(content=''):
    transforms = [
                  AutoLink()
                  ]
    for transform in transforms:
      content = transform.run(content)
    return content
