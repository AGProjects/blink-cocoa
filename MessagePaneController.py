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
                    NSAlert,
                    NSAlertFirstButtonReturn,
                    NSApp,
                    NSAttributedString,
                    NSBezierPath,
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
                    NSNoImage,
                    NSLineBreakByTruncatingTail,
                    NSLineBreakByTruncatingMiddle,
                    NSBezelBorder,
                    NSMenu,
                    NSMenuItem,
                    NSPasteboard,
                    NSScrollView,
                    NSStringPboardType,
                    NSTextView,
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
from Avatars import AvatarView, avatar_image
from util import (otr_enabled_for_account, pgp_enabled_for_account,
                  run_in_gui_thread)
from MessageBubbleView import (transcript_font_size, set_transcript_font_size,
                               FONT_SIZE_STEP, MIN_BODY_FONT_SIZE,
                               MAX_BODY_FONT_SIZE, PlaybackStopButton)
from PlaybackMonitor import PLAYBACK_STATE_CHANGED, playback_monitor


HEADER_HEIGHT = 44.0
AVATAR_SIZE = 28.0
# The contact's avatar drawn in place of the application icon on the PGP
# key panel, at the size an alert draws its icon.
PANEL_AVATAR_SIZE = 64.0
LOCK_SIZE = 14.0
# Drawn if the lock artwork cannot be loaded: the control has to keep its
# place in the header whatever happens to the image files.
LOCK_GLYPH = chr(128274)
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
# Stop whatever is playing, immediately left of the handset. Only there
# while something IS playing: one player serves the whole application, and
# once the user has clicked another contact the bubble that started the
# clip is no longer on screen to stop it. This is the control that is.
#
# Drawn as the play key rather than as a glyph like its neighbours: it is
# the same transport, and the row it sits in is the only place the user
# can reach it once the bubble has gone. A disc wants a little more room
# than a text glyph does.
STOP_BUTTON_W = 20.0
# The local account this conversation sends from: a pill on the info line,
# right after the address. A pill rather than a plain run of text because
# it is also the control that changes the account -- it is the only part
# of that line that is a choice, and looking like a control is what says
# so. Shown only where there is more than one account to choose between.
ACCOUNT_PILL_FONT_SIZE = 10.0
ACCOUNT_PILL_H = 15.0
# Left and right air inside the pill, and the gap between the address and
# the pill that follows it.
ACCOUNT_PILL_PAD = 7.0
ACCOUNT_PILL_GAP = 6.0
# What the account pill is left when the address would otherwise take the
# whole line. Enough to read "From a@b" as a pill rather than as an
# ellipsis; the address gives way first because it is the half that
# truncates gracefully.
ACCOUNT_PILL_MIN_W = 76.0
MONTH_NAMES = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
               'August', 'September', 'October', 'November', 'December')


# The contact list never goes below this, and the transcript never below
# its own minimum -- between them they are what makes the divider stop in
# sensible places instead of letting either side vanish.
LIST_MIN_WIDTH = 274.0
PANE_MIN_WIDTH = 320.0


class AccountPill(NSButton):
    """The account name, drawn in a rounded capsule.

    A button with the border turned off and the capsule drawn by hand:
    a bordered NSButton at this size is a chunky push button that would
    outweigh the name above it, and a borderless one is indistinguishable
    from the address beside it. The capsule is the middle: quiet enough
    for a second line, obviously a thing to click.
    """

    def drawRect_(self, rect):
        bounds = self.bounds()
        radius = bounds.size.height / 2.0
        capsule = NSMakeRect(0.5, 0.5, bounds.size.width - 1.0, bounds.size.height - 1.0)
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(capsule, radius, radius)
        # Pressed reads as a darker fill rather than a different shape, so
        # the line does not jump while the menu is coming up.
        try:
            pressed = bool(self.cell().isHighlighted())
        except Exception:
            pressed = False
        (NSColor.tertiaryLabelColor() if pressed else NSColor.quaternaryLabelColor()).setFill()
        path.fill()
        objc.super(AccountPill, self).drawRect_(rect)


class MessageHeaderView(NSView):
    """The header strip, which re-lays its text when the pane is resized.

    The buttons are pinned to the right edge by their autoresizing masks
    and need no help. The address and the account pill are placed against
    each other from the left, so how much room they have is a question
    only the header can answer, and only once it knows its new width.
    """

    controller = None

    def resizeSubviewsWithOldSize_(self, oldSize):
        objc.super(MessageHeaderView, self).resizeSubviewsWithOldSize_(oldSize)
        controller = self.controller
        if controller is None or getattr(controller, 'accountPill', None) is None:
            return                      # still being built
        try:
            controller._setInfoLine(controller._selected)
        except Exception as e:
            BlinkLogger().log_error('Cannot lay out the header: %s' % e)


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
                # The lock in the header is read off the conversation, and
                # the conversation learns what it is worth after it has been
                # selected: the private key and the peer's public key are
                # loaded when the viewer is built, PGP keys arrive per
                # account after registration, and OTR is negotiated later
                # still. Without these the header kept whatever the lock
                # happened to be at selection time -- typically grey, and
                # grey it stayed for the rest of the session.
                for name in ('ChatStreamOTREncryptionStateChanged',
                             'OTREncryptionDidStop',
                             'PGPEncryptionStateChanged',
                             'PGPPublicKeyReceived',
                             'BlinkConversationAccountChanged'):
                    NotificationCenter().add_observer(self, name=name)
                # The stop button belongs to the pane rather than to the
                # conversation that started the clip, so it has to hear
                # about playback that began -- and ended -- somewhere else.
                NotificationCenter().add_observer(
                    self, name=PLAYBACK_STATE_CHANGED)
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
        # Switching OTR or PGP off for an account takes their items out of
        # the menu, and switching both off takes the lock away entirely --
        # while the conversation they belong to is on screen.
        if ('sms.enable_pgp' in modified or 'sms.enable_otr' in modified
                or 'chat.enable_encryption' in modified):
            self.updateEncryptionWidgets()

    # Encryption can change under a conversation at any time -- and it
    # changes for every conversation, not only the one on screen, so what
    # matters here is not which viewer the notification came from but that
    # the selected one is asked again. updateEncryptionWidgets reads the
    # state off the viewer, so re-reading after somebody else's change
    # costs a lock image and cannot show the wrong session's state.

    @objc.python_method
    def _NH_ChatStreamOTREncryptionStateChanged(self, sender, data):
        self.updateEncryptionWidgets()

    @objc.python_method
    def _NH_OTREncryptionDidStop(self, sender, data):
        self.updateEncryptionWidgets()

    @objc.python_method
    def _NH_PGPEncryptionStateChanged(self, sender, data):
        self.updateEncryptionWidgets()

    @objc.python_method
    def _NH_BlinkConversationAccountChanged(self, sender, data):
        """A conversation moved to another account.

        Not always the user's doing: a message arriving on a different
        account moves the conversation onto it, and the pill has to say
        so without anybody clicking anything.
        """
        if sender is not self._selected:
            return
        self._setInfoLine(sender)
        self.updateEncryptionWidgets(sender)
        self.updateLocationButton(sender)

    @objc.python_method
    def _NH_PGPPublicKeyReceived(self, sender, data):
        # Posted per account: the key that just arrived may be the one the
        # conversation on screen was waiting for.
        self.updateEncryptionWidgets()

    @objc.python_method
    def _NH_BlinkPlaybackStateChanged(self, sender, data):
        self.updateStopPlaybackButton()

    # -- view ------------------------------------------------------------

    @objc.python_method
    def _buildView(self):
        self.view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 480, 395))
        self.view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

        header = MessageHeaderView.alloc().initWithFrame_(
            NSMakeRect(0, 395 - HEADER_HEIGHT, 480, HEADER_HEIGHT))
        header.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        header.controller = self
        self.headerView = header

        self.avatarView = AvatarView.alloc().initWithFrame_(
            NSMakeRect(PAD, (HEADER_HEIGHT - AVATAR_SIZE) / 2.0, AVATAR_SIZE, AVATAR_SIZE))
        header.addSubview_(self.avatarView)

        text_x = PAD + AVATAR_SIZE + PAD
        self.nameLabel = self._label(NSMakeRect(text_x, HEADER_HEIGHT / 2.0, 300, 17),
                                     NSFont.boldSystemFontOfSize_(13), NSColor.labelColor())
        self.nameLabel.setAutoresizingMask_(NSViewWidthSizable)
        header.addSubview_(self.nameLabel)

        self.infoLabel = self._label(NSMakeRect(text_x, HEADER_HEIGHT / 2.0 - 16, 300, 14),
                                     NSFont.systemFontOfSize_(11), NSColor.secondaryLabelColor())
        # Truncated in the MIDDLE, unlike the name above it. This line is an
        # address, and for a Bonjour neighbour an instance id and the
        # computer it runs on -- both ends carry the information and the
        # middle of a uuid carries none, so a tail ellipsis would drop the
        # only human-readable half.
        self.infoLabel.cell().setLineBreakMode_(NSLineBreakByTruncatingMiddle)
        # Sized to its text rather than stretched, because the account pill
        # is placed immediately after it and has to know where it ends.
        header.addSubview_(self.infoLabel)

        self.accountPill = AccountPill.alloc().initWithFrame_(
            NSMakeRect(text_x, HEADER_HEIGHT / 2.0 - 16, 0, ACCOUNT_PILL_H))
        self.accountPill.setBordered_(False)
        self.accountPill.setTarget_(self)
        self.accountPill.setAction_('showAccountMenu:')
        self.accountPill.setHidden_(True)
        header.addSubview_(self.accountPill)

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
        # Draw it grey straight away: the header is built before anything is
        # selected, and a button with no image at all is an invisible one.
        self.updateEncryptionWidgets(None)

        # Text size, immediately left of the lock. Two buttons rather than a
        # menu: making the transcript readable is a thing people do while
        # reading it, and a menu would put two clicks in front of every step.
        button_y = (HEADER_HEIGHT - FONT_BUTTON_H) / 2.0
        large_x = 480 - PAD - LOCK_SIZE - 10.0 - FONT_BUTTON_W
        small_x = large_x - FONT_BUTTON_W
        history_x = small_x - 8.0 - HISTORY_BUTTON_W
        location_x = history_x - 8.0 - LOCATION_BUTTON_W
        call_x = location_x - 8.0 - CALL_BUTTON_W
        stop_x = call_x - 8.0 - STOP_BUTTON_W
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

        self.stopPlaybackButton = PlaybackStopButton.alloc().initWithFrame_(
            NSMakeRect(stop_x, button_y, STOP_BUTTON_W, FONT_BUTTON_H))
        self.stopPlaybackButton.setAutoresizingMask_(
            NSViewMinXMargin | NSViewMinYMargin)
        self.stopPlaybackButton.setToolTip_(
            NSLocalizedString("Stop playing", "Tooltip"))
        self.stopPlaybackButton.setTarget_(self)
        self.stopPlaybackButton.setAction_('stopPlayback:')
        # Only while a clip is playing. Leaving it there greyed out would
        # put a permanent dead button in a row whose other buttons all come
        # and go with what they act on.
        self.stopPlaybackButton.setHidden_(True)
        header.addSubview_(self.stopPlaybackButton)

        # Everything above was framed against a 480pt header; the row is
        # packed for real now that every button in it exists.
        self._layoutHeaderButtons()

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

    # The header buttons, right to left, with the gap each one keeps from
    # its neighbour on the right. Laid out in code rather than left at
    # fixed frames because most of them come and go -- the lock with the
    # account's encryption settings, the pin with the server, the handset
    # with the conversation -- and a hidden button left holding its slot is
    # a hole in the row.
    HEADER_BUTTONS = (('encryptionButton', 'LOCK_SIZE', 0.0),
                      ('fontLargerButton', 'FONT_BUTTON_W', 10.0),
                      ('fontSmallerButton', 'FONT_BUTTON_W', 0.0),
                      ('historyButton', 'HISTORY_BUTTON_W', 8.0),
                      ('locationButton', 'LOCATION_BUTTON_W', 8.0),
                      ('callButton', 'CALL_BUTTON_W', 8.0),
                      ('stopPlaybackButton', 'STOP_BUTTON_W', 8.0))

    @objc.python_method
    def _layoutHeaderButtons(self):
        """Pack the visible header buttons against the right edge."""
        header = getattr(self, 'headerView', None)
        if header is None:
            return
        sizes = globals()
        x = header.frame().size.width - PAD
        placed = False
        for attribute, width_name, gap in self.HEADER_BUTTONS:
            button = getattr(self, attribute, None)
            if button is None:
                continue                # not built yet
            if button.isHidden():
                continue
            width = sizes[width_name]
            if attribute == 'encryptionButton':
                height, y = LOCK_SIZE, (HEADER_HEIGHT - LOCK_SIZE) / 2.0
            else:
                height, y = FONT_BUTTON_H, (HEADER_HEIGHT - FONT_BUTTON_H) / 2.0
            x -= (gap if placed else 0.0) + width
            button.setFrame_(NSMakeRect(x, y, width, height))
            placed = True

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

    # -- account -----------------------------------------------------------

    @objc.python_method
    def _switchableAccounts(self):
        """The accounts a conversation could be moved to.

        Enabled SIP accounts only. Bonjour is left out on purpose: a
        Bonjour conversation is addressed by instance id on the link local
        account and is not the same conversation over SIP, in either
        direction.
        """
        try:
            from sipsimple.account import AccountManager
            return [account for account in AccountManager().get_accounts()
                    if account is not BonjourAccount() and account.enabled]
        except Exception as e:
            BlinkLogger().log_error('Cannot read the account list: %s' % e)
            return []

    @objc.python_method
    def _accountPillApplies(self, viewer):
        """Whether this conversation gets an account pill at all.

        One account is nothing to choose between and a Bonjour
        conversation cannot be moved, so in both cases the pill would be a
        control that says something obvious and does nothing.
        """
        if viewer is None or getattr(viewer, 'account', None) is BonjourAccount():
            return False
        return len(self._switchableAccounts()) > 1

    @objc.python_method
    def _setInfoLine(self, viewer):
        """Write the second line of the header: address, dash, account pill.

        One method for both because they share a line and sit against each
        other: address at the left, then the pill immediately after it.

        The address is given its full width -- sizeToFit rather than a
        measured string, because an NSTextField insets its text inside its
        frame and a frame sized to the glyphs alone comes back with an
        ellipsis on an address that would have fitted.
        """
        text = self._infoTextFor(viewer) if viewer is not None else ''
        # The dash belongs to the pill, not to the address: it is there to
        # separate the two, and there is nothing to separate without it.
        if text and self._accountPillApplies(viewer):
            text = '%s -' % text
        self.infoLabel.setStringValue_(text)

        frame = self.infoLabel.frame()
        try:
            self.infoLabel.sizeToFit()
            width = self.infoLabel.frame().size.width
        except Exception:
            width = frame.size.width

        # Never past the buttons. sizeToFit asks the text how wide it would
        # LIKE to be, and for a Bonjour conversation that is an instance id
        # plus a computer name -- wider than the header -- so the label was
        # laid straight across the call, history, text-size and lock
        # buttons and drew its address behind them.
        room = max(0.0, self._headerContentRightEdge() - frame.origin.x)
        if self._accountPillApplies(viewer):
            # The pill sits after the address on the same line, so the room
            # it needs comes out of the address's share rather than being
            # taken from it afterwards.
            room = max(0.0, room - ACCOUNT_PILL_GAP - ACCOUNT_PILL_MIN_W)
        width = min(width, room)
        self.infoLabel.setFrame_(
            NSMakeRect(frame.origin.x, frame.origin.y, width, frame.size.height))
        # What was trimmed is still readable on hover.
        self.infoLabel.setToolTip_(text or None)

        self._fitNameLabel()
        self.updateAccountPill(viewer)

    @objc.python_method
    def _fitNameLabel(self):
        """Stop the first line at the buttons too.

        It is width-sizable, so it grows with the pane and would otherwise
        run under them on a long name in a narrow window -- the same
        overlap the line below it had, one line up.
        """
        label = getattr(self, 'nameLabel', None)
        if label is None:
            return
        frame = label.frame()
        width = max(0.0, self._headerContentRightEdge() - frame.origin.x)
        if abs(width - frame.size.width) < 0.5:
            return
        label.setFrame_(
            NSMakeRect(frame.origin.x, frame.origin.y, width, frame.size.height))

    @objc.python_method
    def _headerContentRightEdge(self):
        """Where the header text has to stop: the leftmost visible button."""
        header = getattr(self, 'headerView', None)
        if header is None:
            return 480.0 - PAD
        edge = header.frame().size.width - PAD
        for attribute, _width, _gap in self.HEADER_BUTTONS:
            button = getattr(self, attribute, None)
            if button is None or button.isHidden():
                continue
            edge = min(edge, button.frame().origin.x)
        return edge - PAD

    @objc.python_method
    def updateAccountPill(self, viewer=None):
        """Name the account this conversation sends from, where it matters.

        One account is nothing to choose between and a Bonjour
        conversation cannot be moved at all, so in both cases the pill
        would be a control that says something obvious and does nothing.
        """
        viewer = viewer or self._selected
        show = self._accountPillApplies(viewer)

        self.accountPill.setHidden_(not show)
        if not show:
            return

        account = str(viewer.account.id)
        # "From" inside the pill: on its own an address in a header is
        # read as the person being written to, which is precisely the
        # address sitting next to it. The word is what separates them.
        title = NSLocalizedString("From %s", "Label") % account
        font = NSFont.systemFontOfSize_(ACCOUNT_PILL_FONT_SIZE)
        self.accountPill.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(
                title, {NSFontAttributeName: font,
                        NSForegroundColorAttributeName: NSColor.secondaryLabelColor()}))
        self.accountPill.setToolTip_(
            NSLocalizedString("Messages are sent from %s. Click to send from another account.",
                              "Tooltip") % account)

        try:
            text_width = NSAttributedString.alloc().initWithString_attributes_(
                title, {NSFontAttributeName: font}).size().width
        except Exception:
            text_width = len(title) * ACCOUNT_PILL_FONT_SIZE * 0.6

        # Immediately after the address, not against the right edge: the
        # two are one statement about this conversation and belong
        # together. Only what is left before the buttons caps it, and only
        # in a pane too narrow to hold both.
        info = self.infoLabel.frame()
        x = info.origin.x + info.size.width + (ACCOUNT_PILL_GAP if info.size.width else 0.0)
        width = text_width + 2 * ACCOUNT_PILL_PAD
        room = max(0.0, self._headerContentRightEdge() - x)
        self.accountPill.setFrame_(
            NSMakeRect(x, info.origin.y - 1.0, min(width, room), ACCOUNT_PILL_H))

    @objc.IBAction
    def showAccountMenu_(self, sender):
        viewer = self._selected
        if viewer is None:
            return

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        title = menu.addItemWithTitle_action_keyEquivalent_(
            NSLocalizedString("Send messages from:", "Menu item"), None, '')
        title.setEnabled_(False)

        current = getattr(viewer, 'account', None)
        for account in self._switchableAccounts():
            item = menu.addItemWithTitle_action_keyEquivalent_(
                str(account.id), 'accountMenuAction:', '')
            item.setTarget_(self)
            item.setRepresentedObject_(str(account.id))
            item.setState_(1 if account is current else 0)
            # The account it already sends from stays in the list, ticked,
            # because that is what says which one it is -- but it is not
            # something to click.
            item.setEnabled_(account is not current)

        try:
            origin = NSPoint(0, sender.frame().size.height + 2.0)
            menu.popUpMenuPositioningItem_atLocation_inView_(None, origin, sender)
        except Exception as e:
            BlinkLogger().log_error('Cannot show the account menu: %s' % e)

    @objc.IBAction
    def accountMenuAction_(self, sender):
        viewer = self._selected
        if viewer is None:
            return

        wanted = str(sender.representedObject() or '')
        account = next((a for a in self._switchableAccounts() if str(a.id) == wanted), None)
        if account is None:
            BlinkLogger().log_error('Account %s is no longer available' % wanted)
            return

        try:
            changed = viewer.setAccount(account)
        except Exception as e:
            BlinkLogger().log_error('Cannot move the conversation to %s: %s' % (wanted, e))
            return

        if not changed:
            return

        # The header names the account, and the lock is per account too --
        # a conversation that can be PGP encrypted from one account may
        # not be from another.
        self._setInfoLine(viewer)
        self.updateEncryptionWidgets(viewer)
        self.updateLocationButton(viewer)

    # -- encryption --------------------------------------------------------

    @objc.python_method
    def _publicKeyForViewer(self, viewer):
        """(armoured public key, 8-character key ID) for the peer, or (None, None).

        The key ID is the checksum Sylk Mobile prints and the one the Edit
        Contact panel shows -- public_key_short_checksum, the single
        derivation all three read from, because an ID computed a second way
        would compare equal to nothing.
        """
        if viewer is None:
            return (None, None)
        uri = str(getattr(viewer, 'remote_uri', '') or '').strip()
        if not uri:
            return (None, None)
        try:
            from resources import ApplicationData
            from MessageHost import public_key_short_checksum
            path = os.path.join(ApplicationData.get('keys'), '%s.pubkey' % uri)
            if not os.path.exists(path):
                return (None, None)
            with open(path, 'rb') as key_file:
                data = key_file.read()
        except Exception as e:
            BlinkLogger().log_error('Cannot read the public key of %s: %s' % (uri, e))
            return (None, None)
        if not data:
            return (None, None)
        return (data.decode('utf-8', 'replace'), public_key_short_checksum(data))

    @objc.IBAction
    def showPublicKey_(self, sender):
        """The peer's public key, to look at and to copy.

        The same panel Sylk Mobile has: the key ID large enough to read out
        loud, the armoured key underneath, and a Copy button -- comparing
        the ID against the other device is what makes the key worth
        anything, and that is done out of band, by a human.
        """
        viewer = self._selected
        key_text, checksum = self._publicKeyForViewer(viewer)
        if not key_text:
            return
        name = viewer.display_name or viewer.remote_uri
        copied = False
        while True:
            alert = NSAlert.alloc().init()
            alert.setMessageText_(
                NSLocalizedString("PGP key ID %s", "Window title") % (checksum or '?'))
            alert.setInformativeText_(
                NSLocalizedString("Copied to the clipboard.", "Label") if copied else
                NSLocalizedString("The public key of %s. The key ID above is the one "
                                  "the other device shows for the same key: if they "
                                  "match, it is the same key.", "Label") % name)

            frame = NSMakeRect(0, 0, 460, 240)
            text = NSTextView.alloc().initWithFrame_(frame)
            text.setEditable_(False)
            text.setSelectable_(True)
            text.setVerticallyResizable_(True)
            text.setHorizontallyResizable_(False)
            text.setAutoresizingMask_(NSViewWidthSizable)
            text.setFont_(NSFont.userFixedPitchFontOfSize_(10.0))
            text.setString_(key_text)

            scroll = NSScrollView.alloc().initWithFrame_(frame)
            scroll.setHasVerticalScroller_(True)
            scroll.setBorderType_(NSBezelBorder)
            scroll.setDocumentView_(text)
            alert.setAccessoryView_(scroll)

            # The contact in place of the application icon: the panel is
            # about one person's key, and the Blink logo says nothing about
            # whose it is. Falls back to the logo if there is no avatar to
            # draw.
            try:
                avatar = avatar_image(self._owner.iconPathForURI(str(viewer.remote_uri)),
                                      self.contactNameFor(viewer), PANEL_AVATAR_SIZE)
            except Exception as e:
                BlinkLogger().log_error('Cannot draw the avatar for the key panel: %s' % e)
                avatar = None
            if avatar is not None:
                alert.setIcon_(avatar)

            alert.addButtonWithTitle_(NSLocalizedString("Copy", "Button title"))
            alert.addButtonWithTitle_(NSLocalizedString("Close", "Button title"))
            try:
                response = alert.runModal()
            except Exception as e:
                BlinkLogger().log_error('Cannot show the public key of %s: %s'
                                        % (getattr(viewer, 'remote_uri', None), e))
                return
            if response != NSAlertFirstButtonReturn:
                return
            # Copy leaves the panel open -- the key ID is what the user is
            # here to read, and closing it the moment they copy the key
            # would take it away mid-comparison.
            try:
                board = NSPasteboard.generalPasteboard()
                board.declareTypes_owner_(
                    NSArray.arrayWithObject_(NSStringPboardType), self)
                board.setString_forType_(key_text, NSStringPboardType)
                copied = True
            except Exception as e:
                BlinkLogger().log_error('Cannot copy the public key: %s' % e)
                return

    @objc.python_method
    def _pgpActive(self, viewer):
        """Whether messages in this conversation are PGP encrypted.

        The conversation's own flag first, and then the three things that
        actually decide it -- PGP on for the account, our private key, the
        remote party's public key -- because the flag is also written per
        message: one plaintext message clears it, while the keys that make
        everything we send encrypted are still sitting there. What the lock
        reports is the state of the conversation, so a key arriving turns
        it green and a plaintext message does not turn it grey.
        """
        if viewer is None:
            return False
        if getattr(viewer, 'pgp_encrypted', False):
            return True
        try:
            account = getattr(viewer, 'account', None)
            if not pgp_enabled_for_account(account):
                return False
            private_key = getattr(viewer, 'private_key', None) \
                or getattr(getattr(account, 'sms', None), 'private_key', None)
            return bool(private_key) and bool(getattr(viewer, 'public_key', None))
        except Exception:
            return False

    @objc.python_method
    def _encryptionAvailability(self, viewer):
        """(OTR usable, PGP usable) for one conversation.

        Both are per account, and an account with a mechanism switched off
        must not be offered it: a menu item that cannot work is worse than
        no item, because the user clicks it and nothing happens. The
        exception in both directions is a conversation that is already
        encrypted -- what is running stays visible and, for OTR, stays
        switchable off, so that changing a setting can never trap somebody
        inside a session they cannot end.
        """
        if viewer is None:
            return (False, False)
        try:
            from sipsimple.configuration.settings import SIPSimpleSettings
            account = getattr(viewer, 'account', None)
            otr = getattr(viewer, 'encryption', None)
            otr_active = bool(getattr(otr, 'active', False))
            otr_available = otr_active or (
                bool(SIPSimpleSettings().chat.enable_encryption)
                and otr_enabled_for_account(account))
            pgp_available = self._pgpActive(viewer) or pgp_enabled_for_account(account)
            return (bool(otr_available), bool(pgp_available))
        except Exception as e:
            BlinkLogger().log_error('Cannot read the encryption settings: %s' % e)
            return (False, False)

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
        if menu.numberOfItems() == 0:
            # Both mechanisms off for this account: the button should be
            # hidden already, and an empty menu popping up would be the
            # visible half of a bug rather than a feature.
            return
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
        """The encryption menu for one conversation.

        Only what the account can actually do gets in: with OTR off for the
        account (or off globally in Chat preferences) no OTR item is built,
        with PGP off no PGP item, and with both off the menu comes out
        empty -- which is the same condition that hides the lock, so the
        empty menu is never reachable.
        """
        otr = viewer.encryption
        name = viewer.display_name or viewer.remote_uri
        otr_available, pgp_available = self._encryptionAvailability(viewer)

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        if otr_available:
            # A session that IS encrypted always keeps its deactivate item,
            # whatever the settings now say, so that turning a setting off
            # can never trap the user inside an OTR session.
            self._encryptionItem(
                menu,
                NSLocalizedString("Deactivate OTR encryption for this session", "Menu item")
                if otr.active else
                NSLocalizedString("Activate OTR encryption for this session", "Menu item"),
                1)

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
                # OTR is not journalled, and that is a property of the
                # session worth stating where it is switched on.
                note = menu.addItemWithTitle_action_keyEquivalent_(
                    NSLocalizedString("Messages are not stored on the server", "Menu item"),
                    None, '')
                note.setEnabled_(False)

            self._encryptionItem(menu, NSLocalizedString("About OTR protocol", "Menu item"), 7)

        if pgp_available:
            if menu.numberOfItems():
                menu.addItem_(NSMenuItem.separatorItem())
            if self._pgpActive(viewer):
                self._encryptionItem(
                    menu, NSLocalizedString("PGP encryption active", "Menu item"), 9, enabled=False)
            checksum = self._publicKeyForViewer(viewer)[1]
            if checksum:
                # Its own action rather than a tag: this one opens a panel
                # here instead of asking the conversation to do something.
                item = menu.addItemWithTitle_action_keyEquivalent_(
                    NSLocalizedString("PGP key ID %s", "Menu item") % checksum,
                    'showPublicKey:', '')
                item.setTarget_(self)
                item.setEnabled_(True)

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
        self._layoutHeaderButtons()

    @objc.python_method
    def updateCallButton(self, viewer):
        """Show the handset only while a conversation is on screen."""
        button = getattr(self, 'callButton', None)
        if button is None:
            return
        button.setHidden_(viewer is None)
        self._layoutHeaderButtons()

    @objc.python_method
    def updateStopPlaybackButton(self):
        """Show the stop square only while something is playing.

        Deliberately not asked whether the clip belongs to the conversation
        on screen: it is one player for the whole application, and the case
        this button exists for is the clip that is still playing in a
        conversation the user has already clicked away from.
        """
        button = getattr(self, 'stopPlaybackButton', None)
        if button is None:
            return
        button.setHidden_(not playback_monitor().is_playing())
        self._layoutHeaderButtons()

    @objc.IBAction
    def stopPlayback_(self, sender):
        playback_monitor().stop()

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
            # Nothing is selected in its place. The only way a conversation
            # is removed from the pane is the user deleting it, and jumping
            # to whoever happens to be first in the list answers a question
            # they did not ask: they deleted a conversation, they did not
            # ask to read another one, and the next thing they see should
            # not be somebody else's messages appearing where the deleted
            # ones were. The pane shows its "choose a contact" state and
            # waits.
            self._selected = None
            self.selectViewer(None)
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
        """Remote typing indicator for one conversation.

        Only the selected conversation has a header to draw in; the state
        itself is held by the manager, keyed by URI, and the contact rows
        show the rest. Which is also why this reads it back rather than
        taking `flag`: the header has to be right when the user SELECTS a
        conversation someone is already typing at, and nothing will repeat
        that state until the sender's next refresh a minute later.
        """
        if viewer is not self._selected:
            return
        self._setInfoLine(viewer)

    @objc.python_method
    def updateEncryptionWidgets(self, viewer=None):
        """Set the lock to the state of the conversation.

        Green for OTR verified or PGP encrypted, red for an OTR session
        whose peer is not verified yet, grey for everything else -- off,
        not negotiated, or a state that could not be read. Grey rather
        than gone: the lock used to be cleared to no image, which on a
        borderless image-only button means it vanishes, so the header lost
        a control and said nothing about whether the conversation was
        protected.

        It is hidden in exactly one case, decided by the account rather
        than by the conversation: both OTR and PGP switched off, where
        there is no state to report and nothing in the menu to act on.
        """
        viewer = viewer or self._selected
        otr_available, pgp_available = self._encryptionAvailability(viewer)
        if not otr_available and not pgp_available:
            # Nothing this account can do, or nothing selected: a lock that
            # opens an empty menu says less than no lock at all. This is the
            # ONLY case in which it goes away -- an encryption state that is
            # merely off or unknown keeps the grey lock.
            self.encryptionButton.setHidden_(True)
            self._layoutHeaderButtons()
            return

        name = 'locked-gray.png'
        tooltip = NSLocalizedString("Encryption", "Tooltip")
        if viewer is not None:
            tooltip = NSLocalizedString("Not encrypted", "Tooltip")
            try:
                otr = getattr(viewer, 'encryption', None)
                if getattr(otr, 'active', False):
                    # Red until the remote identity has been verified, green
                    # once it has -- the same distinction the bubbles draw.
                    # Ignoring `verified` here left the header claiming an
                    # unverified session while every bubble said otherwise.
                    if getattr(otr, 'verified', False):
                        name = 'locked-green.png'
                        tooltip = NSLocalizedString("Encrypted and verified", "Tooltip")
                    else:
                        name = 'locked-red.png'
                        tooltip = NSLocalizedString("Encrypted, remote identity not verified", "Tooltip")
                elif self._pgpActive(viewer):
                    name = 'locked-green.png'
                    tooltip = NSLocalizedString("Encrypted with PGP", "Tooltip")
            except Exception:
                # A state we cannot read is not a state we may claim: keep
                # the grey lock rather than guess at a coloured one.
                name = 'locked-gray.png'
                tooltip = NSLocalizedString("Encryption state is unknown", "Tooltip")

        image = None
        try:
            from resources import Resources
            from AppKit import NSImage
            image = NSImage.alloc().initWithContentsOfFile_(Resources.get(name))
        except Exception:
            image = None

        if image is not None:
            # The lock artwork is far larger than the header; a borderless
            # NSButton draws it at natural size, which is why it came out as
            # a huge lock floating over the drawer.
            image.setSize_(NSMakeSize(LOCK_SIZE, LOCK_SIZE))
            self.encryptionButton.setImagePosition_(NSImageOnly)
            self.encryptionButton.setTitle_('')
            self.encryptionButton.setImage_(image)
        else:
            # Missing artwork is not a reason for the control to vanish
            # either: a text lock keeps the menu reachable.
            self.encryptionButton.setImage_(None)
            self.encryptionButton.setImagePosition_(NSNoImage)
            self.encryptionButton.setAttributedTitle_(
                NSAttributedString.alloc().initWithString_attributes_(
                    LOCK_GLYPH,
                    {NSFontAttributeName: NSFont.systemFontOfSize_(LOCK_SIZE - 2.0),
                     NSForegroundColorAttributeName: NSColor.secondaryLabelColor()}))

        # Dimmed, not gone, while there is nothing to report. It stays
        # clickable as long as a conversation is selected, because the menu
        # behind it is what turns encryption on.
        self.encryptionButton.setAlphaValue_(1.0 if name != 'locked-gray.png' else 0.45)
        self.encryptionButton.setEnabled_(viewer is not None)
        self.encryptionButton.setHidden_(False)
        self.encryptionButton.setToolTip_(tooltip)
        self._layoutHeaderButtons()

    # -- selection --------------------------------------------------------

    @objc.python_method
    def selectViewer(self, viewer):
        if viewer is self._selected:
            # Not a no-op for the header: the same conversation can be
            # re-selected after its encryption has moved on (a key that
            # arrived, an OTR session that came up), and the lock has to
            # agree with the conversation rather than with when it was
            # last switched to.
            self.updateEncryptionWidgets(viewer)
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
            self.avatarView.setAvatar(None, '')
            self.updateEncryptionWidgets(None)
            self.updateLocationButton(None)
            self.updateCallButton(None)
            self._setInfoLine(None)
            BlinkLogger().log_info('Message pane: %s -> nothing selected'
                                   % self._conversationLabel(previous))
            return

        from MessageHost import load_trace_tick, load_trace_bucket
        _t = load_trace_tick()
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

        load_trace_bucket('- show content', _t)
        _t = load_trace_tick()
        # Resolved once and used twice: the header label and the avatar's
        # initials are the same name, and working it out walks the address
        # book.
        name = self.contactNameFor(viewer)
        load_trace_bucket('-- contact name', _t)
        _t = load_trace_tick()
        self.nameLabel.setStringValue_(name)
        self._loadAvatarFor(viewer, name)
        load_trace_bucket('- avatar', _t)
        _t = load_trace_tick()
        # Opening a conversation with no key for the other party is the one
        # moment worth asking the server for one: the user is about to
        # write to this address, and a key that arrives turns the lock
        # green before the first message rather than after it. Guarded to
        # one request per address per launch, so clicking down a contact
        # list does not send a lookup per click.
        try:
            viewer.requestPublicKeyIfMissing()
        except AttributeError:
            pass                        # a viewer that does not do PGP
        except Exception as e:
            BlinkLogger().log_error('Cannot look up the public key of %s: %s'
                                    % (getattr(viewer, 'remote_uri', None), e))
        load_trace_bucket('- key request', _t)
        _t = load_trace_tick()
        self.updateEncryptionWidgets(viewer)
        self.updateLocationButton(viewer)
        self.updateCallButton(viewer)
        load_trace_bucket('- header widgets', _t)
        _t = load_trace_tick()
        # Last: the pill is placed against the end of the address, and how
        # much room the line has depends on which buttons ended up visible.
        self._setInfoLine(viewer)
        load_trace_bucket('- info line', _t)

        # One line per switch, saying what the pane is showing now and how
        # much of it: which conversation went, which came, the key its rows
        # are actually filed under, the account it is on, and the number of
        # messages the transcript is holding. A conversation that comes up
        # empty, or one whose key is not the one the history was written
        # under, is otherwise indistinguishable from one that has nothing
        # in it -- and the two need different answers.
        BlinkLogger().log_info('Message pane: %s -> %s, %s'
                               % (self._conversationLabel(previous),
                                  self._conversationLabel(viewer, name),
                                  self._messagesInViewSummary(viewer)))

        if self.isConversationVisible(viewer):
            self.conversationBecameVisible(viewer)
        else:
            # Selecting a conversation IS reading it. conversationBecameVisible
            # is gated on the window being key and the pane being up, which is
            # right for starting the read-receipt queue and wrong for the
            # badge: the user has just picked this conversation out of the
            # list and is looking at the transcript, so a counter still
            # sitting on the row is telling them about messages that are on
            # the screen in front of them.
            self._unread.pop(viewer, None)
            self._clearUnread(viewer)

    @objc.python_method
    def _conversationLabel(self, viewer, name=None):
        """How one conversation is named in the switching log.

        The name AND the key: a Bonjour neighbour is shown by name and
        filed under an instance id, so a line with only one of the two
        cannot be matched against what the history did.
        """
        if viewer is None:
            return 'nothing'
        try:
            key = str(viewer.conversation_peer_uri())
        except Exception:
            key = str(getattr(viewer, 'remote_uri', '') or '?')
        if name is None:
            name = str(getattr(viewer, 'display_name', '') or '')
        account = ''
        try:
            account = str(getattr(viewer.account, 'id', '') or '')
        except Exception:
            account = str(getattr(viewer, 'local_uri', '') or '')
        label = '%s <%s>' % (name, key) if name and name != key else key
        return '%s on %s' % (label, account) if account else label

    @objc.python_method
    def _messagesInViewSummary(self, viewer):
        """How much of a conversation is on screen, for the switching log."""
        controller = getattr(viewer, 'chatViewController', None)
        if controller is None:
            return 'no transcript'
        rendered = 0
        try:
            rendered = len(controller.rendered_messages)
        except Exception:
            rendered = 0
        try:
            first, last, in_view = controller.loadedMessageRange()
        except Exception:
            return '%d bubble(s) in view' % rendered
        notes = max(rendered - in_view, 0)
        summary = '%d message(s) in view' % in_view
        if notes:
            summary += ', %d system note(s)' % notes
        if in_view and first is not None and last is not None:
            try:
                summary += ', %s - %s' % (controller._rangeStamp(first),
                                          controller._rangeStamp(last))
            except Exception:
                summary += ', %s - %s' % (first, last)
        return summary

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
            # Looking, not filing: this runs every time the header is drawn,
            # and the fallbacks below already cover an address the address
            # book has never heard of.
            contact = SMSWindowManager().getContact(uri, create=False)
        except Exception:
            contact = None
        name = getattr(contact, 'name', None) if contact is not None else None
        if name or getattr(viewer, 'display_name', ''):
            name = str(name or viewer.display_name)
            # Discovery labels a Bonjour neighbour "Name (computer)". The
            # computer is on the line below now, so carrying it here says it
            # twice in two lines and spends the title's width on the half
            # that is already answered.
            if getattr(viewer, 'account', None) is BonjourAccount():
                try:
                    from ContactListModel import bonjour_display_name
                except Exception:
                    return name
                # The user's own name for this neighbour wins over the one
                # their machine announces, here as in the contact list.
                return bonjour_display_name(getattr(viewer, 'instance_id', None), name) or name
            return name
        # Last resort. Through the viewer so a Bonjour conversation falls
        # back to its instance id rather than to a link-local address that
        # has already changed, or to the loopback placeholder a
        # conversation restored from history carries.
        try:
            return str(viewer.display_remote_uri())
        except AttributeError:
            return uri

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
        self._setInfoLine(viewer)
        self._loadAvatarFor(viewer)

    @objc.python_method
    def viewerForURI(self, uri):
        """A conversation open in this pane with that address, or None.

        Canonical comparison, not a string one: the addresses reaching
        here come from contacts, notifications and history rows, with and
        without the sip: scheme and its parameters.
        """
        try:
            from SMSWindowManager import SMSWindowManager
            canonical = SMSWindowManager()._canonical_uri
        except Exception:
            canonical = lambda value: str(value).lower().strip()
        key = canonical(uri)
        for viewer in self._viewers:
            if canonical(getattr(viewer, 'remote_uri', '')) == key:
                return viewer
        return None

    @objc.python_method
    def _infoTextFor(self, viewer):
        try:
            from SMSWindowManager import SMSWindowManager
            manager = SMSWindowManager()
            typing = manager.isComposingForURI(manager.conversationKeyFor(viewer))
        except Exception:
            typing = False
        if typing:
            return NSLocalizedString("is typing...", "Label")
        # The account is not in this string: it is the pill that follows it.
        # Asked of the viewer rather than read off remote_uri, because a
        # Bonjour neighbour is shown by instance id -- the address they are
        # answering on is a link-local one that changes between sessions,
        # and for a conversation restored from history it is a placeholder.
        try:
            return str(viewer.display_remote_uri())
        except AttributeError:
            return str(viewer.remote_uri)

    @objc.python_method
    def _loadAvatarFor(self, viewer, name=None):
        """The same avatar the contact list draws, at the same size.

        Name as well as path: with no photograph the header shows the
        contact's initials on their own colour, which is what the row they
        clicked to get here shows and what mobile shows.
        """
        from MessageHost import load_trace_tick, load_trace_bucket
        if name is None:
            name = self.contactNameFor(viewer)
        try:
            _t = load_trace_tick()
            path = self._owner.iconPathForURI(str(viewer.remote_uri))
            load_trace_bucket('-- icon path', _t)
            _t = load_trace_tick()
            self.avatarView.setAvatarPath(path, name)
            load_trace_bucket('-- set avatar', _t)
        except Exception as e:
            BlinkLogger().log_error('Cannot load the conversation avatar: %s' % e)
            self.avatarView.setAvatar(None, '')

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
            manager = SMSWindowManager()
            # The key the badge and the rows are filed under, which for a
            # Bonjour neighbour is their instance id rather than the
            # link-local address the viewer holds. Clearing by the address
            # marked nothing read and popped nothing off the counter, so the
            # badge stayed on the row however often it was opened.
            cleared = manager.clearUnreadMessages(manager.conversationKeyFor(viewer))
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
