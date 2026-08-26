# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

"""Shared, renderer-independent half of a chat transcript.

Everything a conversation view needs that is *not* drawing: the composer and
its typing timers, transcript search, the scroll-back-in-time state machine
and the encryption-ended banner. Drawing itself belongs to a subclass --
NativeChatViewController, which stacks MessageBubbleViews inside a
MessageListView.

There used to be a second subclass in all but name: this class rendered into
a WebKit WebView by evaluating JavaScript against ChatView.html. That is
gone. The methods it used to implement are listed under "renderer contract"
below as no-ops, so a subclass that forgets one degrades to a missing bubble
rather than an AttributeError mid-transcript.
"""

__all__ = ['ChatInputTextView', 'ChatViewController', 'processHTMLText',
           'MSG_STATE_SENDING', 'MSG_STATE_SENT', 'MSG_STATE_FAILED', 'MSG_STATE_FAILED_LOCAL', 'MSG_STATE_DELIVERED', 'MSG_STATE_DEFERRED', 'MSG_STATE_DISPLAYED']

import datetime
import objc
import os
import re
import time
import urllib.request, urllib.parse, urllib.error

from AppKit import NSCommandKeyMask, NSDragOperationNone, NSDragOperationCopy, NSFilenamesPboardType, NSShiftKeyMask, NSTextDidChangeNotification
from Foundation import NSArray, NSDate, NSLocalizedString, NSMakeRange, NSNotificationCenter, NSObject, NSTextView, NSTimer

from SmileyManager import SmileyManager
from util import escape_html, run_in_gui_thread
from BlinkLogger import BlinkLogger

MSG_STATE_SENDING      = "sending"      # middleware told us the message is being sent
MSG_STATE_SENT         = "sent"         # middleware told us the message was sent the next SIP hop
MSG_STATE_FAILED       = "failed"       # msg delivery failed (either SIP next hop or end-user using IMDN)
MSG_STATE_FAILED_LOCAL = "failed_local" # msg sent failed (either a a local timeout or a DNS failure)
MSG_STATE_DEFERRED     = "deferred"     # msg delivered to a server but deferred for later delivery
MSG_STATE_DELIVERED    = "delivered"    # msg successfully delivered to end-user (IMDN support required)
MSG_STATE_DISPLAYED    = "displayed"    # msg was read on end-user device (IMDN support required)

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
    try:
        content = content.decode() if isinstance(content, bytes) else content
    except UnicodeDecodeError:
        return ''

    if is_html:
        content = urlify(content)
        content = content.replace('\n', '')
        content = content.replace('\\', '&#92;')

    result = []
    tokens = _url_pattern.split(content)
    for token in tokens:
        if not is_html and _url_pattern_exact.match(token):
            type, d, rest = token.partition(":")
            url = type + d + urllib.parse.quote(rest.encode('utf-8'), "/%?&=;:,@+$#!")
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

    @objc.python_method
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
        elif self.isEditable():
            objc.super(ChatInputTextView, self).keyDown_(event)


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


    delegate = objc.IBOutlet() # ChatController
    account = None
    rendered_messages = set()
    finishedLoading = False
    search_text = None
    related_messages = []
    show_related_messages = False

    expandSmileys = True

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

    scrolling_back = False

    last_sender = None
    previous_msgid = ""

    textWasPasted = False

    @property
    def sessionController(self):
        return self.delegate.sessionController

    @objc.python_method
    def resetRenderedMessages(self):
        self.rendered_messages=[]

    def setAccount_(self, account):
        self.account = account

    def awakeFromNib(self):
        """Wire the composer. The transcript view is the subclass's business."""
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
    @run_in_gui_thread
    def searchMessages_(self, sender):
        for message in self.rendered_messages:
            self.unmarkFound(message.msgid)

        self.search_text = str(self.searchMessagesBox.stringValue()).strip() or None

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

    def setHandleScrolling_(self, scrolling):
        self.handle_scrolling = scrolling

    def appendAttributedString_(self, content):
        storage = self.inputText.textStorage()
        storage.beginEditing()
        storage.appendAttributedString_(content)
        storage.endEditing()

    @objc.python_method
    def showEncryptionFinishedConfirmationDialog(self):
        self.continueWithoutEncryptionCheckbox.setHidden_(False)
        self.encryptionDisabledWarningLabel.setHidden_(False)
        self.inputText.setSelectable_(False)
        self.inputText.setEditable_(False)

    @objc.python_method
    def hideEncryptionFinishedConfirmationDialog(self):
        self.continueWithoutEncryptionCheckbox.setHidden_(True)
        self.encryptionDisabledWarningLabel.setHidden_(True)
        self.inputText.setSelectable_(True)
        self.inputText.setEditable_(True)

    @objc.IBAction
    def confirmWithoutEncryption_(self, sender):
        self.hideEncryptionFinishedConfirmationDialog()
        self.delegate.stream.encryption.stop()

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

    @objc.python_method
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

    def isScrolling_(self, scrollTop):
        if not self.handle_scrolling:
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
            #self.scrolling_zoom_factor += 1
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

    @objc.python_method
    def close(self):
        # memory clean up
        self.rendered_messages = []
        self.pending_messages = {}
        self.view.removeFromSuperview()
        if self.inputText:
            self.inputText.setOwner(None)
            self.inputText.removeFromSuperview()
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


    # -- renderer contract -------------------------------------------------
    #
    # Implemented by NativeChatViewController. Defined here as no-ops so that
    # the shared code above -- searchMessages_ in particular -- can call them
    # without knowing which renderer is underneath, and so a subclass that
    # misses one loses a bubble instead of raising mid-transcript.

    @objc.python_method
    def _notRendered(self, what):
        BlinkLogger().log_debug('%s has no renderer for %s' % (self.__class__.__name__, what))

    @objc.python_method
    def htmlBoxVisible(self, msgid):
        self._notRendered('htmlBoxVisible')

    @objc.python_method
    def htmlBoxHidden(self, msgid):
        self._notRendered('htmlBoxHidden')

    @objc.python_method
    def markFound(self, msgid):
        self._notRendered('markFound')

    @objc.python_method
    def unmarkFound(self, msgid):
        self._notRendered('unmarkFound')

    @objc.python_method
    def updateEncryptionLock(self, msgid, encryption=None):
        self._notRendered('updateEncryptionLock')

    @objc.python_method
    def markMessage(self, msgid, state, private=False):
        self._notRendered('markMessage')

    @objc.python_method
    def clear(self):
        self._notRendered('clear')

    @objc.python_method
    def showSystemMessage(self, content, timestamp=None, is_error=False, call_id='0', before=False):
        self._notRendered('showSystemMessage')

    @objc.python_method
    def showMessage(self, call_id, msgid, direction, sender, icon_path, content, timestamp,
                    is_html=False, state='', recipient='', is_private=False, history_entry=False,
                    media_type='chat', encryption=None, before=False):
        self._notRendered('showMessage')

    @objc.python_method
    def showLocationMessage(self, call_id, msgid, direction, sender, icon_path, latitude,
                            longitude, accuracy, maps_url, timestamp, state='', is_private=False,
                            history_entry=False, encryption=None, before=False, destination=None,
                            status_text=None, track=None, point_timestamp=None):
        self._notRendered('showLocationMessage')

    @objc.python_method
    def updateLocationMessage(self, msgid, latitude, longitude, accuracy, destination=None, timestamp=None):
        self._notRendered('updateLocationMessage')

    @objc.python_method
    def setLocationMessageStatus(self, msgid, text):
        self._notRendered('setLocationMessageStatus')

    @objc.python_method
    def toggleSmileys(self, expandSmileys):
        self._notRendered('toggleSmileys')

    @objc.python_method
    def updateMessage(self, msgid, content, is_html, expandSmileys):
        self._notRendered('updateMessage')

    @objc.python_method
    def scrollToBottom(self):
        self._notRendered('scrollToBottom')

    @objc.python_method
    def scrollToId(self, id):
        self._notRendered('scrollToId')

    @objc.python_method
    def startRendering(self):
        """Tell the delegate the transcript is ready to be filled.

        Replaces setContentFile_(), which used to hand ChatView.html to the
        WebView; the delegate's chatViewDidLoad_ callback -- what kicks off
        history replay -- hung off that load finishing.
        """
        self._notRendered('startRendering')

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
