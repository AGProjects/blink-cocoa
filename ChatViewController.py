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
import tempfile
import time
import urllib.request, urllib.parse, urllib.error

from AppKit import (NSBitmapImageRep, NSCommandKeyMask, NSDragOperationNone,
                    NSDragOperationCopy, NSFilenamesPboardType, NSMenu,
                    NSMenuItem, NSOffState, NSOnState,
                    NSPasteboard, NSPasteboardTypePNG, NSPasteboardTypeTIFF,
                    NSShiftKeyMask, NSSpellChecker, NSTextDidChangeNotification,
                    NSURLPboardType)
try:
    from AppKit import NSPNGFileType
except ImportError:
    # Renamed NSBitmapImageFileTypePNG in newer SDKs. The value is the
    # constant itself, and it is only ever handed straight back to AppKit --
    # not worth a composer that refuses to load.
    NSPNGFileType = 4
from Foundation import NSArray, NSDate, NSLocale, NSLocalizedString, NSMakeRange, NSNotificationCenter, NSObject, NSTextView, NSTimer, NSURL

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

# What language the shared spell checker is currently set to, as far as we
# know.
#
# macOS keeps one NSSpellChecker for the whole application -- the language is
# a property of that checker, not of a text view -- so "a language per chat"
# can only mean "the language the checker holds while this chat's composer has
# the keyboard". Every composer re-asserts its own language when it takes
# focus and while it is typed into; this remembers what was asserted last so
# the common case costs a string comparison instead of a round trip into
# AppKit. '' means no chat has claimed it yet.
_active_spell_language = ''

# Identifies our own item in the composer's contextual menu.
CHAT_LANGUAGE_MENU_TAG = 8901


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


def pasteboard_files(pboard=None):
    """Real files on the pasteboard -- what the Finder puts there.

    Preferred over the picture below whenever both are present, which is
    what copying a PNG in the Finder produces: the file has the name the
    user knows it by, and the picture would arrive as "Pasted image".
    """
    pboard = pboard or NSPasteboard.generalPasteboard()
    types = pboard.types()
    names = []
    if types.containsObject_(NSFilenamesPboardType):
        names = pboard.propertyListForType_(NSFilenamesPboardType) or []
    elif types.containsObject_(NSURLPboardType):
        url = NSURL.URLFromPasteboard_(pboard)
        if url is not None and url.isFileURL():
            names = [url.path()]
    return [str(name) for name in names if os.path.isfile(str(name))]


def pasteboard_has_picture(pboard=None):
    """Whether there is image data on the board. Looks, writes nothing."""
    pboard = pboard or NSPasteboard.generalPasteboard()
    types = pboard.types()
    return bool(types.containsObject_(NSPasteboardTypePNG)
                or types.containsObject_(NSPasteboardTypeTIFF))


def pasteboard_picture(pboard=None):
    """A screenshot, or anything else copied as an image, written to a file.

    macOS puts a screenshot on the board as image DATA with no file behind
    it, so there is nothing to attach until one is written. PNG is taken as
    it stands and TIFF is converted, because TIFF is what the older
    applications put there and nobody wants a 20 MB transfer for a
    screenshot.

    The file is left in the temporary directory: the transfer copies what
    it is given into its own cache, and this copy has no meaning to the
    user once it has been sent.
    """
    pboard = pboard or NSPasteboard.generalPasteboard()
    types = pboard.types()
    data = None
    if types.containsObject_(NSPasteboardTypePNG):
        data = pboard.dataForType_(NSPasteboardTypePNG)
    elif types.containsObject_(NSPasteboardTypeTIFF):
        tiff = pboard.dataForType_(NSPasteboardTypeTIFF)
        rep = NSBitmapImageRep.imageRepWithData_(tiff) if tiff else None
        if rep is not None:
            data = rep.representationUsingType_properties_(NSPNGFileType, {})
    if not data:
        return []

    # Named the way macOS names a screenshot, because that is usually what
    # this is and it is the name the recipient sees.
    name = 'Pasted image %s.png' % time.strftime('%Y-%m-%d at %H.%M.%S')
    path = os.path.join(tempfile.gettempdir(), name)
    if not data.writeToFile_atomically_(path, True):
        BlinkLogger().log_error('Cannot write the pasted picture to %s' % path)
        return []
    return [path]


def pasteboard_attachments(pboard=None):
    """Everything on the pasteboard that could be sent as a file."""
    pboard = pboard or NSPasteboard.generalPasteboard()
    return pasteboard_files(pboard) or pasteboard_picture(pboard)


def pasteboard_can_attach(pboard=None):
    """Whether a paste would produce an attachment. Looks, writes nothing."""
    try:
        pboard = pboard or NSPasteboard.generalPasteboard()
        return bool(pasteboard_has_picture(pboard) or pasteboard_files(pboard))
    except Exception:
        return False


class ChatInputTextView(NSTextView):
    owner = None
    maxLength = None
    # The language for a conversation with no contact to store it on. Lives
    # as long as the window does.
    sessionLanguage = ''

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

    def validateUserInterfaceItem_(self, item):
        """Keep Edit > Paste alive for a picture.

        This is a PLAIN TEXT view (the nib says importsGraphics="NO"), so
        NSTextView looks at a pasteboard holding nothing but image data,
        finds no type it can read, and disables the Paste item. The menu
        item is what owns Cmd-V -- keyDown_ below swallows every Command
        combination it does not know -- so a disabled item means the
        keystroke is never dispatched at all and paste_ is never called.
        That is why the same Cmd-V works in a rich text editor and does
        nothing here.
        """
        try:
            action = item.action()
            if action in ('paste:', b'paste:') and self._canPasteAsTransfer():
                return True
            if action in ('selectChatLanguage:', b'selectChatLanguage:'):
                return True
        except Exception:
            pass
        try:
            return objc.super(ChatInputTextView, self).validateUserInterfaceItem_(item)
        except Exception:
            return True

    @objc.python_method
    def _canPasteAsTransfer(self):
        """Whether a paste right now would produce a file transfer.

        Asked during menu validation, so it only looks -- nothing is
        written and nothing is sent until the paste actually happens.
        """
        delegate = getattr(self.owner, 'delegate', None) if self.owner else None
        if getattr(delegate, 'sendFiles', None) is None:
            return False
        return pasteboard_can_attach()

    @objc.python_method
    def _pasteAsTransfer(self):
        """Send whatever is on the pasteboard as a file. True if we did.

        False for anything that is not a file or a picture -- plain text,
        an empty board, a conversation that cannot send files -- which is
        what puts the paste back on the text view.

        "True if we did" means the paste was taken over, not that anything
        was sent: the preview the owner puts up is allowed to come back
        with nothing, and a cancelled paste must not then fall through and
        drop a file path into the composer as text.
        """
        owner = self.owner
        confirm = getattr(owner, 'confirmAndSendFiles', None) if owner else None
        if confirm is None:
            return False
        delegate = getattr(owner, 'delegate', None)
        if getattr(delegate, 'sendFiles', None) is None:
            return False

        try:
            paths = pasteboard_attachments()
        except Exception as e:
            BlinkLogger().log_error('Cannot read the pasteboard: %s' % e)
            return False
        if not paths:
            return False

        confirm(paths)
        return True

    def paste_(self, sender):
        """Cmd-V into the composer: a file if the board holds one, else text.

        Reached from the Edit menu and from keyDown_ below. A picture or a
        file becomes a transfer the owner confirms; everything else is
        pasted as plain text, which is what this view holds -- the nib says
        importsGraphics="NO", so the inherited paste_ would consult types it
        cannot read and quietly do nothing.
        """
        if self._pasteAsTransfer():
            return
        self.pasteAsPlainText_(sender)

    def keyDown_(self, event):
        self._applyChatLanguage()
        if event.keyCode() == 36 and (event.modifierFlags() & NSShiftKeyMask):
            self.insertText_('\r\n')
        elif (event.modifierFlags() & NSCommandKeyMask):
            keys = event.characters()
            key = keys.lower() if keys else ''
            if key == 'i' and self.owner.delegate.sessionController.info_panel is not None:
                self.owner.delegate.sessionController.info_panel.toggle()
            elif key == 'v' and self.isEditable():
                # Cmd-V is normally consumed by the Edit menu's Paste item
                # before it ever reaches a key handler -- but only while that
                # item is ENABLED, and for a plain text view it is disabled
                # whenever the board holds something this view cannot read
                # (an image, a file from the Finder). The keystroke then fell
                # through to here, where every unrecognised Command
                # combination is swallowed, so Cmd-V did nothing at all.
                # Handled explicitly instead of widening the swallow: the
                # rest of the Command space is left exactly as it was.
                self.paste_(None)
        elif self.isEditable():
            objc.super(ChatInputTextView, self).keyDown_(event)

    # -- The language this conversation is written in -------------------------
    #
    # Spell checking and autocorrect in a chat client are only useful if they
    # follow the person being written to: the same machine writes Dutch to one
    # contact and English to the next, and a checker that guesses from a
    # half-typed line gets it wrong precisely when the line is short, which in
    # a chat is always. So the language is stored on the contact and asserted
    # by whichever composer has the keyboard.
    #
    # Chosen from the composer's own contextual menu, and nothing changes for
    # a conversation nobody has chosen for: those keep macOS guessing, which
    # is what they do today.

    def becomeFirstResponder(self):
        became = objc.super(ChatInputTextView, self).becomeFirstResponder()
        if became:
            self._applyChatLanguage()
        return became

    @objc.python_method
    def _languageContact(self):
        """The stored contact whose language this composer follows.

        None for a conversation with nothing to store it on -- an unknown URI,
        a bonjour peer, a conference room -- which then keeps its language for
        as long as the window is open and no longer.
        """
        owner = self.owner
        delegate = getattr(owner, 'delegate', None) if owner is not None else None
        if delegate is None:
            return None
        blink_contact = getattr(delegate, 'contact', None)
        if blink_contact is None:
            session = getattr(delegate, 'sessionController', None)
            blink_contact = getattr(session, 'contact', None)
        contact = getattr(blink_contact, 'contact', None)
        if contact is None or not hasattr(contact, 'chat_language'):
            return None
        return contact

    @objc.python_method
    def chatLanguage(self):
        """What this conversation is set to.

        '' when nothing was chosen, 'off', 'auto', or a spell checker
        language code.
        """
        contact = self._languageContact()
        if contact is not None:
            try:
                return contact.chat_language or ''
            except Exception:
                return ''
        return self.sessionLanguage or ''

    @objc.python_method
    def setChatLanguage(self, language):
        language = str(language) if language else ''
        self.sessionLanguage = language
        contact = self._languageContact()
        if contact is None:
            return
        try:
            contact.chat_language = language or None
            contact.save()
        except Exception as e:
            BlinkLogger().log_error('Cannot save the chat language: %s' % e)

    @objc.python_method
    def _applyChatLanguage(self):
        """Put this conversation's language on the shared spell checker.

        Called on every path that can bring text into this view, because the
        checker is shared: the composer next door may have pointed it at
        another language since this one last typed.

        A conversation nobody has chosen a language for keeps exactly the
        behaviour it has today -- whatever the nib turned on, checked against
        the language macOS identifies -- so this only ever asserts the
        automatic setting back, never the checking itself. Off and a named
        language are deliberate choices, and those do switch the composer.
        """
        global _active_spell_language

        try:
            wanted = self.chatLanguage()
        except Exception:
            return

        if wanted == 'off':
            if self.isContinuousSpellCheckingEnabled():
                self.setContinuousSpellCheckingEnabled_(False)
            if self.isAutomaticSpellingCorrectionEnabled():
                self.setAutomaticSpellingCorrectionEnabled_(False)
            return

        language = wanted or 'auto'
        if language != _active_spell_language:
            checker = NSSpellChecker.sharedSpellChecker()
            try:
                if language == 'auto':
                    checker.setAutomaticallyIdentifiesLanguages_(True)
                else:
                    checker.setAutomaticallyIdentifiesLanguages_(False)
                    if not checker.setLanguage_(language):
                        BlinkLogger().log_warning('No spell checking dictionary for %s' % language)
                _active_spell_language = language
            except Exception as e:
                BlinkLogger().log_error('Cannot set the spell checking language: %s' % e)
                return
            # What is already in the composer was marked up against the old
            # language, and AppKit does not revisit it on its own.
            if self.isContinuousSpellCheckingEnabled():
                self.setContinuousSpellCheckingEnabled_(False)
                self.setContinuousSpellCheckingEnabled_(True)

        if not wanted:
            return

        if not self.isContinuousSpellCheckingEnabled():
            self.setContinuousSpellCheckingEnabled_(True)
        if not self.isAutomaticSpellingCorrectionEnabled():
            self.setAutomaticSpellingCorrectionEnabled_(True)

    @objc.python_method
    def _languageDisplayName(self, code, locale):
        """'nl' -> 'Dutch', 'en_GB' -> 'English (United Kingdom)'.

        Spell checker codes are locale identifiers, so the locale can name
        them; anything it will not name is shown as the code itself rather
        than dropped, because a dictionary the user has installed should
        appear in the menu whatever it is called.
        """
        identifier = code.replace('-', '_')
        for selector in ('localizedStringForLocaleIdentifier_', 'localizedStringForLanguageCode_'):
            method = getattr(locale, selector, None)
            if method is None:
                continue
            try:
                name = method(identifier)
            except Exception:
                name = None
            if name:
                return name
        return code

    @objc.python_method
    def _chatLanguageMenu(self):
        current = self.chatLanguage() or 'auto'
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        def add(title, value):
            item = menu.addItemWithTitle_action_keyEquivalent_(title, 'selectChatLanguage:', '')
            item.setTarget_(self)
            item.setRepresentedObject_(value)
            item.setEnabled_(True)
            item.setState_(NSOnState if value == current else NSOffState)

        add(NSLocalizedString("Off", "Menu item"), 'off')
        add(NSLocalizedString("Automatic", "Menu item"), 'auto')
        menu.addItem_(NSMenuItem.separatorItem())

        locale = NSLocale.currentLocale()
        try:
            languages = list(NSSpellChecker.sharedSpellChecker().availableLanguages() or [])
        except Exception as e:
            BlinkLogger().log_error('Cannot list the spell checking languages: %s' % e)
            languages = []
        names = dict((code, self._languageDisplayName(code, locale)) for code in languages)
        for code in sorted(languages, key=lambda c: names[c].lower()):
            add(names[code], code)
        return menu

    def menuForEvent_(self, event):
        try:
            menu = objc.super(ChatInputTextView, self).menuForEvent_(event)
        except Exception:
            menu = None
        if menu is None:
            menu = NSMenu.alloc().init()
        try:
            # NSTextView hands back a menu it owns, and a second right click
            # gets the same object: without this the submenu would be appended
            # once per click.
            item = menu.itemWithTag_(CHAT_LANGUAGE_MENU_TAG)
            if item is None:
                menu.addItem_(NSMenuItem.separatorItem())
                item = menu.addItemWithTitle_action_keyEquivalent_(
                    NSLocalizedString("Chat Language", "Menu item"), None, '')
                item.setTag_(CHAT_LANGUAGE_MENU_TAG)
            item.setSubmenu_(self._chatLanguageMenu())
        except Exception as e:
            BlinkLogger().log_error('Cannot build the chat language menu: %s' % e)
        return menu

    def selectChatLanguage_(self, sender):
        try:
            language = sender.representedObject()
        except Exception:
            return
        self.setChatLanguage(language)
        self._applyChatLanguage()


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
    def confirmAndSendFiles(self, paths, title=None):
        """Show what is about to be sent, and send it if the user agrees.

        The single gate every source goes through -- the file panel, the
        camera, the clipboard, Cmd-V. Sending straight from the source is
        what made a mistyped Cmd-V an irreversible thing done to somebody
        else's conversation.

        Returns the number of files sent.
        """
        delegate = self.delegate
        send = getattr(delegate, 'sendFiles', None)
        if send is None or not paths:
            return 0

        try:
            from AttachmentPreview import confirm_attachments
        except Exception as e:
            # A preview that will not import must not cost the feature it
            # was added to guard: the file still goes, as it did before.
            BlinkLogger().log_error('Cannot load the attachment preview: %s' % e)
            confirm_attachments = None

        if confirm_attachments is not None:
            paths = confirm_attachments(paths, self.attachmentPreviewParent(),
                                        title or self.attachmentPreviewTitle())
            if not paths:
                BlinkLogger().log_info('Attachment cancelled')
                return 0

        try:
            return send(paths) or 0
        except Exception as e:
            BlinkLogger().log_error('Cannot send the attachments: %s' % e)
            return 0

    @objc.python_method
    def attachmentPreviewParent(self):
        """The window to centre the preview on, or None for the screen."""
        for view in (self.inputText, self.outputView, self.view):
            try:
                window = view.window() if view is not None else None
            except Exception:
                window = None
            if window is not None:
                return window
        return None

    @objc.python_method
    def attachmentPreviewTitle(self):
        """Who this is going to, said in the preview's own words."""
        delegate = self.delegate
        name = (getattr(delegate, 'display_name', None)
                or getattr(delegate, 'remote_uri', None))
        if not name:
            return None
        return NSLocalizedString("Send to %s", "Label") % name

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
    def invalidateTimers(self):
        # Every timer scheduled with self as target holds a retain on
        # self. They have to go on the teardown path, before the release
        # in close() -- if one is still live when the last reference
        # drops, the final release happens inside CFRunLoop's timer
        # teardown (_timerRelease) and PyObjC's bridged dealloc then
        # crashes with CFRetain(NULL) on a half-dead instance.
        if self.typingTimer:
            self.typingTimer.invalidate()
            self.typingTimer = None
        if self.scrollingTimer:
            self.scrollingTimer.invalidate()
            self.scrollingTimer = None

    @objc.python_method
    def close(self):
        # memory clean up
        self.rendered_messages = []
        self.pending_messages = {}
        self.invalidateTimers()
        self.view.removeFromSuperview()
        if self.inputText:
            self.inputText.setOwner(None)
            self.inputText.removeFromSuperview()
        self.release()

    def dealloc(self):
        # No timer invalidation here -- see invalidateTimers.
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
