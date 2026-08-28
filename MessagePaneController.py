# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

"""The messages view beside the contact list in the main window.

A split view, not the audio drawer: an NSDrawer is capped at the width of
the window it hangs off, so a wide transcript dragged the contact list wide
with it and could not be dragged wider than the list. In a split view the
transcript takes whatever width it is given and the list stays narrow.
This controller owns the messages side and implements the host protocol from
MessageHost.py, so SMSWindowManagerClass can hand it conversations exactly as
it hands them to the legacy tabbed SMSWindowController.

The view is built in code rather than loaded from a nib: it is a header, a
container and a label, and every outlet is one more thing to mis-wire.

Conversations are added but not created here -- nothing arriving over the
wire builds a conversation any more. The user clicks a contact, that creates
the viewer, and it lands here.
"""

from AppKit import (NSRoundedBezelStyle,
                    NSApp,
                    NSAttributedString,
                    NSBox,
                    NSDragOperationCopy,
                    NSDragOperationNone,
                    NSFilenamesPboardType,
                    NSButton,
                    NSCenterTextAlignment,
                    NSColor,
                    NSFont,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSImageOnly,
                    NSImageView,
                    NSLineBreakByTruncatingTail,
                    NSMenu,
                    NSMenuItem,
                    NSSplitView,
                    NSSplitViewDividerStyleThin,
                    NSTextField,
                    NSView,
                    NSViewHeightSizable,
                    NSViewMaxYMargin,
                    NSViewMinXMargin,
                    NSViewMinYMargin,
                    NSViewWidthSizable)
from Foundation import (NSArray,
                        NSLocalizedString,
                        NSMakeRect,
                        NSMakeSize,
                        NSObject,
                        NSPoint)

import os

import objc

from application.notification import NotificationCenter, IObserver
from application.python import Null
from sipsimple.account import BonjourAccount
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from util import run_in_gui_thread
from MessageBubbleView import (transcript_font_size, set_transcript_font_size,
                               FONT_SIZE_STEP, MIN_BODY_FONT_SIZE,
                               MAX_BODY_FONT_SIZE)


HEADER_HEIGHT = 44.0
AVATAR_SIZE = 28.0
LOCK_SIZE = 14.0
PAD = 8.0
# Safari's text-size pair: a small A and a big A, sitting beside the lock.
FONT_BUTTON_W = 20.0
FONT_BUTTON_H = 20.0
FONT_BUTTON_SMALL = 10.0
FONT_BUTTON_LARGE = 15.0
# The history navigator: a calendar that drills year -> month -> day.
HISTORY_BUTTON_W = 22.0
HISTORY_GLYPH = chr(128197)
# Location, immediately left of the calendar: a pin, which is the glyph the
# map bubbles and the system notes already use for this ("\U0001F4CD started
# sharing live location"), so the button and what it produces are visibly
# the same feature.
LOCATION_BUTTON_W = 22.0
LOCATION_GLYPH = chr(128205)
# Calling, immediately left of the pin: the same handset the rest of the
# application uses for an audio session, so the button says what it starts
# without a label.
#
# Drawn a couple of points smaller than its neighbours so it does not LOOK
# bigger than them. Emoji share an em box but not how much of it they ink:
# the pin and the calendar leave air around themselves, the handset fills
# its box corner to corner. Matched by point size they come out visibly
# uneven, so what is matched here is the drawn glyph instead. One constant,
# because it is a thing to nudge by eye.
CALL_BUTTON_W = 22.0
CALL_GLYPH = chr(128222)
CALL_GLYPH_SIZE = FONT_BUTTON_LARGE - 4.0
MONTH_NAMES = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
               'August', 'September', 'October', 'November', 'December')


# The contact list never goes below this, and the transcript never below
# its own minimum -- between them they are what makes the divider stop in
# sensible places instead of letting either side vanish.
LIST_MIN_WIDTH = 274.0
PANE_MIN_WIDTH = 320.0


class MessagePaneSplitView(NSSplitView):
    """Contact list on the left, conversation on the right.

    Its own delegate, so the geometry rules live with the view instead of
    being spread across the window controller. The rule is deliberately not
    the proportional one NSSplitView does by default: the contact list keeps
    the width the user gave it and the transcript absorbs every change,
    which is what makes the window behave like a chat client rather than a
    pair of panes fighting over a percentage.
    """

    def initWithFrame_(self, frame):
        self = objc.super(MessagePaneSplitView, self).initWithFrame_(frame)
        if self:
            self.setVertical_(True)
            self.setDividerStyle_(NSSplitViewDividerStyleThin)
            self.setDelegate_(self)
        return self

    @objc.python_method
    def _bounds(self):
        size = self.frame().size
        return size, max(size.width - self.dividerThickness(), 0.0)

    @objc.python_method
    def _clampListWidth(self, width, room):
        upper = max(LIST_MIN_WIDTH, room - PANE_MIN_WIDTH)
        return min(max(width, LIST_MIN_WIDTH), upper)

    def splitView_constrainMinCoordinate_ofSubviewAt_(self, splitView, proposed, index):
        return max(proposed, LIST_MIN_WIDTH)

    def splitView_constrainMaxCoordinate_ofSubviewAt_(self, splitView, proposed, index):
        size, room = self._bounds()
        return min(proposed, max(LIST_MIN_WIDTH, room - PANE_MIN_WIDTH))

    def splitView_constrainSplitPosition_ofSubviewAt_(self, splitView, proposed, index):
        size, room = self._bounds()
        return self._clampListWidth(proposed, room)

    def splitView_resizeSubviewsWithOldSize_(self, splitView, oldSize):
        views = self.subviews()
        if views.count() < 2:
            self.adjustSubviews()
            return
        left = views.objectAtIndex_(0)
        right = views.objectAtIndex_(1)
        size, room = self._bounds()
        list_width = self._clampListWidth(left.frame().size.width, room)
        left.setFrame_(NSMakeRect(0, 0, list_width, size.height))
        right.setFrame_(NSMakeRect(list_width + self.dividerThickness(), 0,
                                   max(room - list_width, 0.0), size.height))

    @objc.python_method
    def listWidth(self):
        views = self.subviews()
        if views.count() < 1:
            return LIST_MIN_WIDTH
        return views.objectAtIndex_(0).frame().size.width

    @objc.python_method
    def setListWidth(self, width):
        size, room = self._bounds()
        self.setPosition_ofDividerAtIndex_(self._clampListWidth(width, room), 0)


class MessageDropView(NSView):
    """The conversation area, as somewhere to drop files on.

    Registered on the container rather than on the transcript inside it:
    neither the scroll view nor the message list registers for dragged
    types, so a drop anywhere over the conversation walks up to this one
    view -- which is what makes the whole pane the target rather than a
    strip of it.
    """

    controller = None

    def initWithFrame_(self, frame):
        self = objc.super(MessageDropView, self).initWithFrame_(frame)
        if self:
            self.registerForDraggedTypes_(
                NSArray.arrayWithObject_(NSFilenamesPboardType))
        return self

    @objc.python_method
    def _files(self, sender):
        board = sender.draggingPasteboard()
        if not board.types().containsObject_(NSFilenamesPboardType):
            return None
        names = board.propertyListForType_(NSFilenamesPboardType)
        files = [str(name) for name in names or [] if os.path.isfile(str(name))]
        return files or None

    @objc.python_method
    def _isRoundTrip(self, sender):
        """Whether this drag started in the conversation it is over.

        A file dragged out of a bubble and let go over the same transcript
        is somebody changing their mind, not somebody sending the file to
        the person who just sent it to them. Without this it lands as a
        second, identical transfer.

        Dropping it on a DIFFERENT conversation is a real thing to want --
        that is forwarding -- so only the round trip is refused.
        """
        if self.controller is None:
            return False
        try:
            return self.controller.dragCameFromSelectedConversation(
                sender.draggingSource())
        except Exception:
            return False

    def draggingEntered_(self, sender):
        if self.controller is None or not self.controller.canReceiveDroppedFiles():
            return NSDragOperationNone
        if self._isRoundTrip(sender):
            # Refused at the door, so the pointer shows the no-entry cursor
            # and the file springs back rather than appearing to be
            # accepted and then silently doing nothing.
            return NSDragOperationNone
        return NSDragOperationCopy if self._files(sender) else NSDragOperationNone

    def prepareForDragOperation_(self, sender):
        return bool(self._files(sender)) and not self._isRoundTrip(sender)

    def performDragOperation_(self, sender):
        files = self._files(sender)
        if not files or self.controller is None or self._isRoundTrip(sender):
            return False
        return self.controller.sendFiles(files)


@implementer(IObserver)
class MessagePaneController(NSObject):

    def initWithOwner_(self, owner):
        self = objc.super(MessagePaneController, self).init()
        if self:
            self._owner = owner            # ContactWindowController
            self._viewers = []
            self._content_views = {}       # viewer -> its content view
            self._selected = None
            self._unread = {}
            self._buildView()
            # A contact's name and icon are read once, when its conversation
            # is opened. Editing the contact afterwards has to reach the
            # header, or the pane goes on calling someone by the name they
            # were just renamed away from.
            try:
                NotificationCenter().add_observer(self, name='BlinkContactsHaveChanged')
                # The pin's visibility depends on a setting the SERVER
                # fills in: sms.history_url arrives in a message after
                # registration, which can easily be after the user has
                # already clicked a contact. Without this the pin would
                # stay hidden until they clicked away and back.
                NotificationCenter().add_observer(self, name='CFGSettingsObjectDidChange')
            except Exception as e:
                # Worth surviving: without the pane there is no messages UI
                # at all, and the app quietly falls back to the old tabbed
                # window -- a much bigger loss than a stale header name.
                BlinkLogger().log_error('Cannot watch for contact changes: %s' % e)
        return self

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    @objc.python_method
    def _NH_BlinkContactsHaveChanged(self, sender, data):
        self.refreshContactDetails()

    @objc.python_method
    def _NH_CFGSettingsObjectDidChange(self, sender, data):
        # Fires for every setting in the application; only one of them
        # changes anything here, so it is checked before doing any work.
        try:
            modified = data.modified
        except AttributeError:
            return
        if 'sms.history_url' in modified:
            self.updateLocationButton(self._selected)

    # -- view ------------------------------------------------------------

    @objc.python_method
    def _buildView(self):
        self.view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 480, 395))
        self.view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

        header = NSView.alloc().initWithFrame_(NSMakeRect(0, 395 - HEADER_HEIGHT, 480, HEADER_HEIGHT))
        header.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        self.headerView = header

        self.avatarView = NSImageView.alloc().initWithFrame_(
            NSMakeRect(PAD, (HEADER_HEIGHT - AVATAR_SIZE) / 2.0, AVATAR_SIZE, AVATAR_SIZE))
        header.addSubview_(self.avatarView)

        text_x = PAD + AVATAR_SIZE + PAD
        self.nameLabel = self._label(NSMakeRect(text_x, HEADER_HEIGHT / 2.0, 300, 17),
                                     NSFont.boldSystemFontOfSize_(13), NSColor.labelColor())
        self.nameLabel.setAutoresizingMask_(NSViewWidthSizable)
        header.addSubview_(self.nameLabel)

        self.infoLabel = self._label(NSMakeRect(text_x, HEADER_HEIGHT / 2.0 - 16, 300, 14),
                                     NSFont.systemFontOfSize_(11), NSColor.secondaryLabelColor())
        self.infoLabel.setAutoresizingMask_(NSViewWidthSizable)
        header.addSubview_(self.infoLabel)

        self.encryptionButton = NSButton.alloc().initWithFrame_(
            NSMakeRect(480 - PAD - LOCK_SIZE, (HEADER_HEIGHT - LOCK_SIZE) / 2.0, LOCK_SIZE, LOCK_SIZE))
        self.encryptionButton.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
        self.encryptionButton.setBezelStyle_(NSRoundedBezelStyle)
        self.encryptionButton.setBordered_(False)
        self.encryptionButton.setImagePosition_(NSImageOnly)
        self.encryptionButton.setTitle_('')
        self.encryptionButton.setToolTip_(
            NSLocalizedString("Encryption", "Tooltip"))
        self.encryptionButton.setTarget_(self)
        self.encryptionButton.setAction_('showEncryptionMenu:')
        header.addSubview_(self.encryptionButton)

        # Text size, immediately left of the lock. Two buttons rather than a
        # menu: making the transcript readable is a thing people do while
        # reading it, and a menu would put two clicks in front of every step.
        button_y = (HEADER_HEIGHT - FONT_BUTTON_H) / 2.0
        large_x = 480 - PAD - LOCK_SIZE - 10.0 - FONT_BUTTON_W
        small_x = large_x - FONT_BUTTON_W
        history_x = small_x - 8.0 - HISTORY_BUTTON_W
        location_x = history_x - 8.0 - LOCATION_BUTTON_W
        call_x = location_x - 8.0 - CALL_BUTTON_W
        self.fontSmallerButton = self._fontButton(
            NSMakeRect(small_x, button_y, FONT_BUTTON_W, FONT_BUTTON_H),
            FONT_BUTTON_SMALL, 'decreaseFontSize:',
            NSLocalizedString("Smaller text", "Tooltip"))
        self.fontLargerButton = self._fontButton(
            NSMakeRect(large_x, button_y, FONT_BUTTON_W, FONT_BUTTON_H),
            FONT_BUTTON_LARGE, 'increaseFontSize:',
            NSLocalizedString("Larger text", "Tooltip"))
        header.addSubview_(self.fontSmallerButton)
        header.addSubview_(self.fontLargerButton)
        self._updateFontButtons()

        self.historyButton = self._fontButton(
            NSMakeRect(history_x, button_y, HISTORY_BUTTON_W, FONT_BUTTON_H),
            FONT_BUTTON_LARGE - 2.0, 'showHistoryDates:',
            NSLocalizedString("Jump to a date in this conversation", "Tooltip"),
            title=HISTORY_GLYPH)
        header.addSubview_(self.historyButton)

        self.locationButton = self._fontButton(
            NSMakeRect(location_x, button_y, LOCATION_BUTTON_W, FONT_BUTTON_H),
            FONT_BUTTON_LARGE - 2.0, 'showLocationMenu:',
            NSLocalizedString("Location", "Tooltip"),
            title=LOCATION_GLYPH)
        # Hidden until a conversation is selected and its account turns out
        # to be talking to a SylkServer. Starting visible would mean the pin
        # flickering in and out on the first click of a contact.
        self.locationButton.setHidden_(True)
        header.addSubview_(self.locationButton)

        self.callButton = self._fontButton(
            NSMakeRect(call_x, button_y, CALL_BUTTON_W, FONT_BUTTON_H),
            CALL_GLYPH_SIZE, 'startAudioCall:',
            NSLocalizedString("Start an audio call", "Tooltip"),
            title=CALL_GLYPH)
        # Hidden until there is somebody to call, for the same reason the
        # pin is: a button that does nothing is worse than no button.
        self.callButton.setHidden_(True)
        header.addSubview_(self.callButton)

        separator = NSBox.alloc().initWithFrame_(NSMakeRect(0, 395 - HEADER_HEIGHT - 1, 480, 1))
        separator.setBoxType_(2)           # NSBoxSeparator
        separator.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)

        container = MessageDropView.alloc().initWithFrame_(NSMakeRect(0, 0, 480, 395 - HEADER_HEIGHT - 1))
        container.controller = self
        container.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.conversationContainer = container

        self.emptyLabel = self._label(NSMakeRect(0, container.frame().size.height / 2.0 - 10, 480, 20),
                                      NSFont.systemFontOfSize_(13), NSColor.secondaryLabelColor())
        self.emptyLabel.setAlignment_(NSCenterTextAlignment)
        self.emptyLabel.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin | NSViewMaxYMargin)
        self.emptyLabel.setStringValue_(NSLocalizedString("Select a contact to see messages", "Label"))
        container.addSubview_(self.emptyLabel)

        self.view.addSubview_(container)
        self.view.addSubview_(separator)
        self.view.addSubview_(header)

    @objc.python_method
    def _fontButton(self, frame, size, action, tooltip, title='A'):
        button = NSButton.alloc().initWithFrame_(frame)
        button.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
        button.setBordered_(False)
        button.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(
                title, {NSFontAttributeName: NSFont.systemFontOfSize_(size),
                        NSForegroundColorAttributeName: NSColor.secondaryLabelColor()}))
        button.setToolTip_(tooltip)
        button.setTarget_(self)
        button.setAction_(action)
        return button

    @objc.IBAction
    def increaseFontSize_(self, sender):
        self._stepFontSize(FONT_SIZE_STEP)

    @objc.IBAction
    def decreaseFontSize_(self, sender):
        self._stepFontSize(-FONT_SIZE_STEP)

    @objc.python_method
    def _stepFontSize(self, delta):
        """Resize the transcript, and every other one that is open.

        The size belongs to the reader rather than to the conversation, so
        the conversations not on screen are moved too -- otherwise clicking
        a different contact would undo what the user just did.
        """
        size = set_transcript_font_size(transcript_font_size() + delta)
        for viewer in list(self._viewers):
            controller = getattr(viewer, 'chatViewController', None)
            apply_size = getattr(controller, 'applyTranscriptFontSize', None)
            if apply_size is None:
                continue
            try:
                apply_size(size)
            except Exception as e:
                BlinkLogger().log_error('Cannot resize a transcript: %s' % e)
        self._updateFontButtons()

    @objc.python_method
    def _updateFontButtons(self):
        """Grey out whichever end of the range we have reached."""
        try:
            size = transcript_font_size()
            self.fontSmallerButton.setEnabled_(size > MIN_BODY_FONT_SIZE)
            self.fontLargerButton.setEnabled_(size < MAX_BODY_FONT_SIZE)
        except Exception:
            pass

    # -- encryption --------------------------------------------------------

    @objc.IBAction
    def showEncryptionMenu_(self, sender):
        """The encryption menu the tabbed window had, on the lock icon.

        Built here rather than loaded from SMSSession.xib: the pane has no
        nib, and the menu is a dozen items whose state is worked out at
        open time anyway. The tags match the old menu exactly, because the
        conversation still answers them through the same
        userClickedEncryptionMenu_.
        """
        viewer = self._selected
        if viewer is None:
            return
        menu = self._buildEncryptionMenu(viewer)
        try:
            origin = NSPoint(0, sender.frame().size.height + 2.0)
            menu.popUpMenuPositioningItem_atLocation_inView_(None, origin, sender)
        except Exception as e:
            BlinkLogger().log_error('Cannot show the encryption menu: %s' % e)

    @objc.python_method
    def _encryptionItem(self, menu, title, tag, enabled=True, state=False):
        item = menu.addItemWithTitle_action_keyEquivalent_(title, 'encryptionMenuAction:', '')
        item.setTag_(tag)
        item.setTarget_(self)
        item.setEnabled_(bool(enabled))
        item.setState_(1 if state else 0)
        return item

    @objc.python_method
    def _buildEncryptionMenu(self, viewer):
        from sipsimple.configuration.settings import SIPSimpleSettings
        settings = SIPSimpleSettings()
        otr = viewer.encryption
        name = viewer.display_name or viewer.remote_uri

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        if settings.chat.enable_encryption:
            self._encryptionItem(
                menu,
                NSLocalizedString("Deactivate OTR encryption for this session", "Menu item")
                if otr.active else
                NSLocalizedString("Activate OTR encryption for this session", "Menu item"),
                1)
        else:
            self._encryptionItem(
                menu, NSLocalizedString("OTR encryption is disabled in Chat preferences",
                                        "Menu item"), 2, enabled=False)

        if otr.active:
            self._encryptionItem(
                menu, NSLocalizedString("My fingerprint is %s", "Menu item")
                % str(otr.key_fingerprint), 2, enabled=False)
            if otr.peer_fingerprint:
                self._encryptionItem(
                    menu, NSLocalizedString("%s's fingerprint is %s", "Menu item")
                    % (name, otr.peer_fingerprint), 4, enabled=False)
                self._encryptionItem(
                    menu, NSLocalizedString("I trust the remote identity", "Menu item"),
                    5, state=otr.verified)
                self._encryptionItem(
                    menu, NSLocalizedString("Validate the identity of %s", "Menu item") % name,
                    6)
            # OTR is not journalled, and that is a property of the session
            # worth stating where it is switched on.
            note = menu.addItemWithTitle_action_keyEquivalent_(
                NSLocalizedString("Messages are not stored on the server", "Menu item"),
                None, '')
            note.setEnabled_(False)

        menu.addItem_(NSMenuItem.separatorItem())
        self._encryptionItem(menu, NSLocalizedString("About OTR protocol", "Menu item"), 7)

        if getattr(viewer, 'pgp_encrypted', False):
            menu.addItem_(NSMenuItem.separatorItem())
            self._encryptionItem(
                menu, NSLocalizedString("PGP encryption active", "Menu item"), 9, enabled=False)

        if '@' in str(viewer.remote_uri):
            self._encryptionItem(
                menu, NSLocalizedString("Lookup PGP public key", "Menu item"), 11)
            self._encryptionItem(
                menu, NSLocalizedString("Send my PGP public key", "Menu item"), 12)
        self._encryptionItem(menu, NSLocalizedString("About PGP protocol", "Menu item"), 10)
        return menu

    @objc.IBAction
    def encryptionMenuAction_(self, sender):
        viewer = self._selected
        if viewer is None:
            return
        try:
            viewer.userClickedEncryptionMenu_(sender)
        except Exception as e:
            BlinkLogger().log_error('Encryption menu action failed: %s' % e)
        self.updateEncryptionWidgets(viewer)

    # -- outgoing files ----------------------------------------------------

    @objc.python_method
    def canReceiveDroppedFiles(self):
        viewer = self._selected
        return viewer is not None and getattr(viewer, 'canSendFiles', lambda: False)()

    @objc.python_method
    def viewerForDragSource(self, source):
        """The conversation a dragged message bubble came from, or None.

        Found by asking which open conversation owns the bubble's renderer
        rather than by giving every bubble a back-pointer to its viewer:
        the bubble already knows its renderer, the pane already knows its
        viewers, and one more reference cycle through a view that is
        created and destroyed by the thousand is not worth the tidier
        lookup.
        """
        renderer = getattr(source, 'renderer', None)
        if renderer is None:
            return None
        for viewer in self._viewers:
            if getattr(viewer, 'chatViewController', None) is renderer:
                return viewer
        return None

    @objc.python_method
    def dragCameFromSelectedConversation(self, source):
        """True when a drag started in the conversation now on screen."""
        viewer = self.viewerForDragSource(source)
        return viewer is not None and viewer is self._selected

    @objc.python_method
    def dragOriginURI(self, source):
        """The address of the conversation a drag started in, or None."""
        viewer = self.viewerForDragSource(source)
        if viewer is None:
            return None
        return str(getattr(viewer, 'remote_uri', '') or '') or None

    @objc.python_method
    def sendFiles(self, paths):
        """Hand dropped or chosen files to the conversation on screen."""
        viewer = self._selected
        if viewer is None or not hasattr(viewer, 'sendFiles'):
            return False
        try:
            return bool(viewer.sendFiles(paths))
        except Exception as e:
            BlinkLogger().log_error('Cannot send the dropped files: %s' % e)
            return False

    # -- history navigator ------------------------------------------------

    # -- location ----------------------------------------------------------

    @objc.python_method
    def sylkServerDetected(self, viewer):
        """Whether this conversation's account is served by a SylkServer.

        The tell is `sms.history_url`: it is not configured by hand but
        arrives in an `application/sylk-api-token` message the server
        sends after registration, alongside the journal token. So a
        non-empty value means a SylkServer introduced itself on this
        account, which is exactly the question -- location sharing rides
        the server's relay and its push, and against a plain SIP proxy
        the pin would offer something that quietly goes nowhere.
        """
        account = getattr(viewer, 'account', None) if viewer is not None else None
        if account is None:
            return False
        try:
            return bool(account.sms.history_url)
        except AttributeError:
            # BonjourAccount, and any account whose settings predate the
            # journal. Neither has a server behind it.
            return False

    @objc.python_method
    def updateLocationButton(self, viewer):
        """Show or hide the pin for the conversation now on screen."""
        button = getattr(self, 'locationButton', None)
        if button is None:
            return
        show = viewer is not None and self.sylkServerDetected(viewer)
        if bool(button.isHidden()) != (not show):
            BlinkLogger().log_debug(
                'Location pin %s for %s' % ('shown' if show else 'hidden',
                                            getattr(viewer, 'remote_uri', None)))
        button.setHidden_(not show)

    @objc.python_method
    def updateCallButton(self, viewer):
        """Show the handset only while a conversation is on screen."""
        button = getattr(self, 'callButton', None)
        if button is None:
            return
        button.setHidden_(viewer is None)

    @objc.IBAction
    def startAudioCall_(self, sender):
        """Call whoever is on screen, from the account they were reached on.

        The conversation's own account rather than the active one: this
        address is the one the contact has been talking to, and calling
        from another shows them somebody they may not recognise.
        """
        viewer = self._selected
        if viewer is None:
            return

        account = getattr(viewer, 'account', None)
        target = getattr(viewer, 'remote_uri', None)
        if account is BonjourAccount():
            # A neighbour is addressed by the whole URI: their user@host is
            # a link-local address carrying a port, and the bare pair would
            # be handed to a proxy that has never heard of them.
            target = str(getattr(viewer, 'target_uri', '') or target)
        if not target:
            BlinkLogger().log_error('Cannot call: the conversation has no address')
            return

        try:
            NSApp.delegate().contactsWindowController.startSessionWithTarget(
                target, media_type='audio',
                local_uri=str(account.id) if account is not None else None,
                selected_contact=getattr(viewer, 'contact', None),
                display_name=getattr(viewer, 'display_name', '') or '')
        except Exception as e:
            BlinkLogger().log_error('Cannot start an audio call to %s: %s' % (target, e))

    @objc.IBAction
    def showLocationMenu_(self, sender):
        """The pin's menu: send mine, or ask for theirs.

        Both items are built every time rather than kept around: whether
        this Mac can produce a fix depends on a system setting and on a
        permission the user may grant while the app is running, and a menu
        built once at launch would still be saying no afterwards.
        """
        viewer = self._selected
        if viewer is None:
            return

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        send = menu.addItemWithTitle_action_keyEquivalent_(
            NSLocalizedString("Send Current Location", "Menu item"),
            'sendCurrentLocation:', '')
        send.setTarget_(self)

        # A reason, in the item's own tooltip, rather than an item that is
        # simply dead: "greyed out and no explanation" is the version of
        # this people report as broken.
        reason = self._locationUnavailableReason()
        if reason:
            send.setEnabled_(False)
            send.setToolTip_(reason)

        request = menu.addItemWithTitle_action_keyEquivalent_(
            NSLocalizedString("Request Location", "Menu item"),
            'requestPeerLocation:', '')
        request.setTarget_(self)

        try:
            origin = NSPoint(0, sender.frame().size.height + 2.0)
            menu.popUpMenuPositioningItem_atLocation_inView_(None, origin, sender)
        except Exception as e:
            BlinkLogger().log_error('Cannot show the location menu: %s' % e)

    @objc.python_method
    def _locationUnavailableReason(self):
        """Why this Mac cannot produce a fix, or None if it can.

        Imported here rather than at module scope so a CoreLocation that
        will not load costs a disabled menu item instead of a pane that
        will not build.
        """
        try:
            from MacLocation import unavailable_reason
        except Exception as e:
            BlinkLogger().log_info('Location support is not available: %s' % e)
            return NSLocalizedString("This build has no location support", "Label")
        try:
            return unavailable_reason()
        except Exception as e:
            BlinkLogger().log_error('Cannot tell whether location is available: %s' % e)
            return NSLocalizedString("Location is not available", "Label")

    @objc.IBAction
    def sendCurrentLocation_(self, sender):
        """Ask macOS where we are, then send it to whoever is on screen.

        A fix takes seconds, so the conversation is remembered and checked
        again when the answer comes: someone who clicks a different contact
        while the Mac is still looking must not have their location sent
        to that one.
        """
        viewer = self._selected
        if viewer is None:
            return
        try:
            from MacLocation import current_location
        except Exception as e:
            BlinkLogger().log_error('Cannot import location support: %s' % e)
            return

        target = viewer
        uri = getattr(viewer, 'remote_uri', '')

        def answered(coords, error):
            if coords is None:
                BlinkLogger().log_error('No location to send to %s: %s' % (uri, error))
                self._noteInConversation(
                    target,
                    NSLocalizedString("Could not get your location: %s", "Label") % error,
                    is_error=True)
                return
            if target not in self._viewers:
                BlinkLogger().log_info('Location arrived after %s was closed; not sent' % uri)
                return
            try:
                target.send_location_once(coords)
            except Exception as e:
                BlinkLogger().log_error('Cannot send a location to %s: %s' % (uri, e))

        self._noteInConversation(
            target, NSLocalizedString("\U0001F4CD Getting your location\u2026", "Label"))
        current_location(answered)

    @objc.IBAction
    def requestPeerLocation_(self, sender):
        """Ask the other side to share theirs. Nothing local is needed."""
        viewer = self._selected
        if viewer is None:
            return
        try:
            viewer.send_location_request()
        except Exception as e:
            BlinkLogger().log_error('Cannot request a location from %s: %s'
                                    % (getattr(viewer, 'remote_uri', '?'), e))

    @objc.python_method
    def _noteInConversation(self, viewer, text, is_error=False):
        """A one-line note in a conversation's transcript, best effort.

        Not persisted: "getting your location" is true for a few seconds
        and meaningless in replayed history, which is why sylk-mobile shows
        the same note through renderSystemMessage rather than storing it.
        """
        try:
            viewer.chatViewController.showSystemMessage(text, is_error=is_error)
        except Exception as e:
            BlinkLogger().log_debug('Cannot show a location note: %s' % e)

    @objc.IBAction
    def showHistoryDates_(self, sender):
        """Ask the conversation which days it has, then put them in a menu.

        The index is read from the database, so it is fetched rather than
        held: a conversation gains days while it is open, and a menu built
        once at launch would go stale the first time someone wrote.
        """
        viewer = self._selected
        if viewer is None or not hasattr(viewer, 'load_history_date_index'):
            return
        button = sender

        def ready(days):
            self._presentHistoryMenu(viewer, days, button)

        viewer.load_history_date_index(ready)

    @objc.python_method
    def _presentHistoryMenu(self, viewer, days, button):
        if viewer is not self._selected:
            return                      # they changed conversation meanwhile

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        latest = menu.addItemWithTitle_action_keyEquivalent_(
            NSLocalizedString("Jump to Latest", "Menu item"), 'jumpToHistoryDate:', '')
        latest.setTarget_(self)
        latest.setRepresentedObject_('')

        if not days:
            menu.addItem_(NSMenuItem.separatorItem())
            empty = menu.addItemWithTitle_action_keyEquivalent_(
                NSLocalizedString("No stored messages", "Menu item"), None, '')
            empty.setEnabled_(False)
        else:
            menu.addItem_(NSMenuItem.separatorItem())
            for year, months in self._groupDays(days):
                year_item = menu.addItemWithTitle_action_keyEquivalent_(year, None, '')
                year_menu = NSMenu.alloc().init()
                year_menu.setAutoenablesItems_(False)
                for month, month_days in months:
                    month_item = year_menu.addItemWithTitle_action_keyEquivalent_(
                        self._monthTitle(month, len(month_days)), 'jumpToHistoryDate:', '')
                    month_item.setTarget_(self)
                    # the month lands on its LAST day, which is where the
                    # month's conversation ended
                    month_item.setRepresentedObject_(month_days[0])
                    day_menu = NSMenu.alloc().init()
                    day_menu.setAutoenablesItems_(False)
                    for day in month_days:
                        day_item = day_menu.addItemWithTitle_action_keyEquivalent_(
                            day, 'jumpToHistoryDate:', '')
                        day_item.setTarget_(self)
                        day_item.setRepresentedObject_(day)
                    year_menu.setSubmenu_forItem_(day_menu, month_item)
                menu.setSubmenu_forItem_(year_menu, year_item)

        try:
            origin = NSPoint(0, button.frame().size.height + 2.0)
            menu.popUpMenuPositioningItem_atLocation_inView_(None, origin, button)
        except Exception as e:
            BlinkLogger().log_error('Cannot show the history menu: %s' % e)

    @objc.python_method
    def _groupDays(self, days):
        """[(year, [(month, [day, ...]), ...]), ...], newest first throughout."""
        years = []
        index = {}
        for day in days:
            parts = str(day).split('-')
            if len(parts) < 2:
                continue
            year, month = parts[0], parts[1]
            if year not in index:
                index[year] = {}
                years.append(year)
            index[year].setdefault(month, []).append(str(day))
        return [(year, [(month, index[year][month])
                        for month in sorted(index[year], reverse=True)])
                for year in years]

    @objc.python_method
    def _monthTitle(self, month, day_count):
        try:
            name = NSLocalizedString(MONTH_NAMES[int(month) - 1], "Month name")
        except (ValueError, IndexError):
            name = str(month)
        return '%s (%d)' % (name, day_count)

    @objc.IBAction
    def jumpToHistoryDate_(self, sender):
        viewer = self._selected
        if viewer is None or not hasattr(viewer, 'jump_to_history_date'):
            return
        target = sender.representedObject()
        viewer.jump_to_history_date(str(target) if target else None)

    @objc.python_method
    def _label(self, frame, font, colour):
        field = NSTextField.alloc().initWithFrame_(frame)
        field.setEditable_(False)
        field.setSelectable_(False)
        field.setBordered_(False)
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setFont_(font)
        field.setTextColor_(colour)
        field.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        field.setStringValue_('')
        return field

    @objc.python_method
    def bringToFront(self, focus=True):
        """Presenting a conversation reveals the messages pane."""
        self._owner.showMessagesPane()
        if focus:
            self._owner.window().makeKeyAndOrderFront_(None)

    # -- host protocol ----------------------------------------------------

    def window(self):
        return self._owner.window()

    @property
    def viewers(self):
        return list(self._viewers)

    @objc.python_method
    def selectedSessionController(self):
        return self._selected

    @objc.python_method
    def addViewer(self, viewer, focusTab=False):
        if viewer in self._viewers:
            if focusTab:
                self.selectViewer(viewer)
            return

        self._viewers.append(viewer)
        content = viewer.getContentView()
        self._content_views[viewer] = content
        content.setHidden_(True)
        content.setFrame_(self.conversationContainer.bounds())
        content.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.conversationContainer.addSubview_(content)
        BlinkLogger().log_debug('Messages pane: added conversation with %s' % viewer.remote_uri)

        if focusTab or self._selected is None:
            self.selectViewer(viewer)

    def removeViewer_(self, viewer):
        content = self._content_views.pop(viewer, None)
        if content is not None:
            content.removeFromSuperview()
        if viewer in self._viewers:
            self._viewers.remove(viewer)
        self._unread.pop(viewer, None)
        if self._selected is viewer:
            self._selected = None
            self.selectViewer(self._viewers[0] if self._viewers else None)
        if not self._viewers:
            # Nothing left to show. A pane standing open on "Select a
            # contact to see messages" after the conversation it was
            # showing has been deleted is a window half full of nothing.
            try:
                self._owner.hideMessagesPane()
            except Exception as e:
                BlinkLogger().log_error('Cannot close the messages pane: %s' % e)

    @objc.python_method
    def noteNewMessageForSession_(self, session):
        if self.isConversationVisible(session):
            self.conversationBecameVisible(session)
            return
        self._unread[session] = self._unread.get(session, 0) + 1
        try:
            from SMSWindowManager import SMSWindowManager
            SMSWindowManager().noteUnreadMessage(session.remote_uri)
        except Exception as e:
            BlinkLogger().log_error('Cannot bump unread for %s: %s' % (session, e))

    @objc.python_method
    def noteNoMessageForSession_(self, session):
        self._unread.pop(session, None)
        self._clearUnread(session)

    @objc.python_method
    def noteView_isComposing_(self, viewer, flag):
        if viewer is not self._selected:
            return
        self.infoLabel.setStringValue_(
            NSLocalizedString("is typing...", "Label") if flag else self._infoTextFor(viewer))

    @objc.python_method
    def updateEncryptionWidgets(self, viewer=None):
        viewer = viewer or self._selected
        image = None
        if viewer is not None:
            try:
                from resources import Resources
                from AppKit import NSImage
                otr = getattr(viewer, 'encryption', None)
                if getattr(otr, 'active', False):
                    # Red until the remote identity has been verified, green
                    # once it has -- the same distinction the bubbles draw.
                    # Ignoring `verified` here left the header claiming an
                    # unverified session while every bubble said otherwise.
                    name = 'locked-green.png' if getattr(otr, 'verified', False) \
                        else 'locked-red.png'
                    image = NSImage.alloc().initWithContentsOfFile_(Resources.get(name))
                elif getattr(viewer, 'pgp_encrypted', False):
                    image = NSImage.alloc().initWithContentsOfFile_(Resources.get('locked-green.png'))
            except Exception:
                image = None

        if image is not None:
            # The lock artwork is far larger than the header; a borderless
            # NSButton draws it at natural size, which is why it came out as
            # a huge lock floating over the drawer.
            image.setSize_(NSMakeSize(LOCK_SIZE, LOCK_SIZE))
        self.encryptionButton.setImage_(image)

    # -- selection --------------------------------------------------------

    @objc.python_method
    def selectViewer(self, viewer):
        if viewer is self._selected:
            return

        previous = self._selected
        if previous is not None:
            content = self._content_views.get(previous)
            if content is not None:
                content.setHidden_(True)
            try:
                previous.not_read_queue_stop()
            except Exception:
                pass

        self._selected = viewer
        self.emptyLabel.setHidden_(viewer is not None)

        if viewer is None:
            self.nameLabel.setStringValue_('')
            self.infoLabel.setStringValue_('')
            self.avatarView.setImage_(None)
            self.updateEncryptionWidgets(None)
            self.updateLocationButton(None)
            self.updateCallButton(None)
            return

        content = self._content_views.get(viewer)
        if content is not None:
            content.setFrame_(self.conversationContainer.bounds())
            content.setHidden_(False)
            # The view was framed while it was hidden, possibly against a
            # container that had no size yet. Anything the conversation
            # placed by hand has to be re-asserted now that it does.
            try:
                viewer.chatViewController.ensureAttachButton()
            except AttributeError:
                pass                    # nothing to stop
            except Exception as e:
                BlinkLogger().log_error('Cannot lay out the composer for %s: %s'
                                        % (viewer.remote_uri, e))

        self.nameLabel.setStringValue_(self.contactNameFor(viewer))
        self.infoLabel.setStringValue_(self._infoTextFor(viewer))
        self._loadAvatarFor(viewer)
        self.updateEncryptionWidgets(viewer)
        self.updateLocationButton(viewer)
        self.updateCallButton(viewer)

        if self.isConversationVisible(viewer):
            self.conversationBecameVisible(viewer)

    @objc.python_method
    def contactNameFor(self, viewer):
        """The name to show for a conversation, as the address book has it now.

        Falls back to whatever the viewer was opened with, and finally to
        the address itself -- an edit that clears the display name should
        leave the URI showing, not an empty header.
        """
        uri = str(getattr(viewer, 'remote_uri', '') or '')
        try:
            from SMSWindowManager import SMSWindowManager
            contact = SMSWindowManager().getContact(uri)
        except Exception:
            contact = None
        name = getattr(contact, 'name', None) if contact is not None else None
        return str(name or getattr(viewer, 'display_name', '') or uri)

    @objc.python_method
    def refreshContactDetails(self):
        """Re-read every open conversation's contact after an edit.

        The name is written back onto the viewer as well as into the
        header: it is what the viewer stamps on outgoing history rows and
        what the header will read again the next time this conversation is
        selected, so leaving it stale would put the old name back on the
        next click.
        """
        for viewer in list(self._viewers):
            try:
                name = self.contactNameFor(viewer)
                if str(getattr(viewer, 'display_name', '') or '') != name:
                    viewer.display_name = name
            except Exception as e:
                BlinkLogger().log_error('Cannot refresh a conversation name: %s' % e)

        viewer = self._selected
        if viewer is None:
            return
        self.nameLabel.setStringValue_(self.contactNameFor(viewer))
        self.infoLabel.setStringValue_(self._infoTextFor(viewer))
        self._loadAvatarFor(viewer)

    @objc.python_method
    def viewerForURI(self, uri):
        canonical = str(uri).lower()
        for viewer in self._viewers:
            if str(viewer.remote_uri).lower() == canonical:
                return viewer
        return None

    @objc.python_method
    def _infoTextFor(self, viewer):
        return str(viewer.remote_uri)

    @objc.python_method
    def _loadAvatarFor(self, viewer):
        try:
            from AppKit import NSImage
            path = self._owner.iconPathForURI(str(viewer.remote_uri))
            self.avatarView.setImage_(NSImage.alloc().initWithContentsOfFile_(path) if path else None)
        except Exception:
            self.avatarView.setImage_(None)

    # -- visibility (drives read receipts and the contact badge) ----------

    @objc.python_method
    def isConversationVisible(self, viewer):
        """Main window key AND drawer open AND showing messages AND selected."""
        if viewer is None or viewer is not self._selected:
            return False
        try:
            return bool(self._owner.isMessagesPaneVisible() and self._owner.window().isKeyWindow())
        except Exception:
            return False

    @objc.python_method
    def conversationBecameVisible(self, viewer):
        if viewer is None:
            return
        self._unread.pop(viewer, None)
        self._clearUnread(viewer)
        try:
            viewer.not_read_queue_start()
        except Exception:
            pass

    @objc.python_method
    def _clearUnread(self, viewer):
        try:
            from SMSWindowManager import SMSWindowManager
            cleared = SMSWindowManager().clearUnreadMessages(viewer.remote_uri)
        except Exception as e:
            BlinkLogger().log_error('Cannot clear unread for %s: %s' % (viewer, e))
            return
        if not cleared:
            return
        # There were unread messages and the user has just looked at them, so
        # the account's other devices should stop showing a badge for this
        # conversation. Only on a real transition: announcing every time a
        # conversation comes into view would be a message per click.
        try:
            viewer.announce_conversation_read()
        except Exception as e:
            BlinkLogger().log_error('Cannot announce the read conversation %s: %s'
                                    % (viewer, e))

    @objc.python_method
    def visibilityChanged(self):
        """Called by the owner on window key / drawer open / view swap."""
        if self.isConversationVisible(self._selected):
            self.conversationBecameVisible(self._selected)
        elif self._selected is not None:
            try:
                self._selected.not_read_queue_stop()
            except Exception:
                pass
