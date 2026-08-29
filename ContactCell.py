# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSCompositeSourceAtop,
                    NSCompositeSourceOver,
                    NSRectFillUsingOperation,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSLineBreakByTruncatingTail,
                    NSParagraphStyleAttributeName)

from Foundation import (NSAttributedString,
                        NSString,
                        NSBezierPath,
                        NSColor,
                        NSDictionary,
                        NSFont,
                        NSImage,
                        NSInsetRect,
                        NSMakeRect,
                        NSMakeSize,
                        NSLocalizedString,
                        NSParagraphStyle,
                        NSTextFieldCell)

import datetime
import os
import objc


def format_last_message_time(stamp):
    """A conversation's last-activity stamp, shaped like the mobile list.

    The time on its own for today, because that is the only part anyone
    reads at a glance; a day name for the past week; a date beyond that.
    Returns '' for anything unusable so the caller can just draw it.
    """
    if stamp is None:
        return ''
    try:
        moment = stamp if isinstance(stamp, datetime.datetime) else None
        if moment is None:
            return ''
        if moment.tzinfo is not None:
            moment = moment.astimezone().replace(tzinfo=None)
        else:
            # stored naive in UTC; show it in the user's own time
            moment = moment.replace(tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None)
        day = moment.date()
        today = datetime.date.today()
        delta = (today - day).days
        if delta <= 0:
            return moment.strftime('%H:%M')
        if delta == 1:
            return NSLocalizedString("Yesterday", "Label")
        if delta < 7:
            return moment.strftime('%a')
        if day.year == today.year:
            return moment.strftime('%d %b')
        return moment.strftime('%d/%m/%y')
    except Exception:
        return ''

from BlinkLogger import BlinkLogger
from Avatars import GLYPH_AVATARS, NO_PHOTO_AVATAR, draw_avatar

from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.account import BonjourAccount

from ContactListModel import presence_status_for_contact, presence_status_icons, BonjourBlinkContact, BlinkOnlineContact, BlinkPresenceContact, BlinkMyselfConferenceContact,BlinkConferenceContact, BlinkHistoryViewerContact, HistoryBlinkContact, SystemAddressBookBlinkContact, LdapSearchResultContact, SearchResultContact


# The typing line. A pencil and a word, because it has to be recognisable
# at a glance next to a name.
#
# U+270E LOWER RIGHT PENCIL rather than U+270F PENCIL: the same object
# drawn on the slant, the way a pencil is held, instead of lying flat on
# the line. It is also the quieter of the two to render -- U+270F carries
# an emoji presentation that has to be turned off with a variation
# selector, and is drawn from the colour font at its own scale until it
# is, while U+270E has no emoji form and simply takes the size and colour
# of the text around it.
#
# An NSString rather than a Python str: these are drawn with
# drawInRect_withAttributes_, an ObjC method a plain str does not have. A
# str here raises AttributeError inside drawWithFrame_inView_, whose
# blanket `except Exception: pass` turns that into a second line that is
# simply not there.
COMPOSING_TEXT = NSString.stringWithString_('\u270e is typing\u2026')


class ContactCell(NSTextFieldCell):
    contact = None
    group = None
    # The unread badge. Small enough to sit on the avatar as a marker
    # rather than compete with it: the count matters far less than the fact
    # that there is one.
    BADGE_HEIGHT = 12.0
    BADGE_FONT_SIZE = 8.0
    lastMessageTime = None
    # True while the other party of this conversation is typing. Drawn on
    # the second line in place of the contact's detail: it is transient
    # text, not a count, so it has no business in the badge corner.
    composing = False
    # The avatar: a circle at the left of the row. It used to sit two points
    # off the window's edge, which was close enough to read as touching it
    # once the picture became a filled circle rather than a rectangle inside
    # a photograph's own white border.
    AVATAR_SIZE = 28.0
    AVATAR_LEFT = 6.0
    AVATAR_TOP = 7.0
    # Where the name and the detail start. Derived, so the margin above can
    # be changed without the text either colliding with the avatar or
    # leaving a gap that grows every time it moves.
    TEXT_LEFT = AVATAR_LEFT + AVATAR_SIZE + 5.0
    # Right-hand gutter owned by the presence status bar, which sits at
    # view width - 6. The time stops short of that; the night icon is moved
    # to the left of the time rather than the time working around it.
    TIME_RIGHT_GUTTER = 12.0
    # absolute x where the text lines must stop, or None for the full width
    contentRightEdge = None
    # absolute x of the time's left edge, or None when no time is drawn.
    # The night icon anchors to this so it lands beside the time instead of
    # underneath it, wherever the time happens to fall.
    timeLeftEdge = None
    timeSize = None
    timeColor = None
    _night_icon_cache = {}
    view = None
    frame = None

    audioIcon = NSImage.imageNamed_("audio_16")
    audioHoldIcon = NSImage.imageNamed_("paused_16")
    chatIcon = NSImage.imageNamed_("pencil_16")
    screenIcon = NSImage.imageNamed_("display_16")
    locationIcon = NSImage.imageNamed_("location")
    nightIcon = NSImage.imageNamed_("moon")

    style = NSParagraphStyle.defaultParagraphStyle().mutableCopy()
    style.setLineBreakMode_(NSLineBreakByTruncatingTail)
    groupAttributes = NSDictionary.dictionaryWithObjectsAndKeys_(NSColor.grayColor(), NSForegroundColorAttributeName, NSFont.boldSystemFontOfSize_(NSFont.labelFontSize()+2), NSFontAttributeName)
    firstLineAttributes = NSDictionary.dictionaryWithObjectsAndKeys_(style, NSParagraphStyleAttributeName, NSColor.labelColor(), NSForegroundColorAttributeName, NSFont.systemFontOfSize_(NSFont.labelFontSize()+3), NSFontAttributeName)
    firstLineAttributes_highlighted = NSDictionary.dictionaryWithObjectsAndKeys_(NSColor.whiteColor(), NSForegroundColorAttributeName, style, NSParagraphStyleAttributeName, NSFont.systemFontOfSize_(NSFont.labelFontSize()+3), NSFontAttributeName)
    secondLineAttributes = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(NSFont.labelFontSize()+2), NSFontAttributeName, NSColor.secondaryLabelColor(), NSForegroundColorAttributeName, style, NSParagraphStyleAttributeName)
    secondLineAttributes_highlighted = NSDictionary.dictionaryWithObjectsAndKeys_( NSFont.systemFontOfSize_(NSFont.labelFontSize()+2), NSFontAttributeName, NSColor.whiteColor(), NSForegroundColorAttributeName, style, NSParagraphStyleAttributeName)

    def setContact_(self, contact):
        self.contact = contact

    def setGroup_(self, group):
        self.group = group

    def setMessageIcon_(self, icon):
        self.messageIcon = icon

    timeAttributes = NSDictionary.dictionaryWithObjectsAndKeys_(
        NSFont.systemFontOfSize_(NSFont.labelFontSize()), NSFontAttributeName,
        NSColor.secondaryLabelColor(), NSForegroundColorAttributeName)
    timeAttributes_highlighted = NSDictionary.dictionaryWithObjectsAndKeys_(
        NSFont.systemFontOfSize_(NSFont.labelFontSize()), NSFontAttributeName,
        NSColor.whiteColor(), NSForegroundColorAttributeName)

    def setLastMessageTime_(self, stamp):
        self.lastMessageTime = stamp

    def setComposing_(self, flag):
        self.composing = bool(flag)
        if self.composing:
            # Cheap and rare -- rows are only redrawn when something about
            # them changed -- and it is the one place that proves the state
            # travelled all the way from the wire to the row that draws it.
            BlinkLogger().log_debug('Typing indicator on the row for %s'
                                    % getattr(self.contact, 'uri', self.contact))

    def setUnreadCount_(self, count):
        try:
            self.unreadCount = int(count or 0)
        except (TypeError, ValueError):
            self.unreadCount = 0

    def drawingRectForBounds_(self, rect):
        return rect

    def cellSize(self):
        if self.contact is None:
            return objc.super(ContactCell, self).cellSize()
        return NSMakeSize(100, 30)

    def drawWithFrame_inView_(self, frame, view):
        self.frame = frame
        self.view = view

        if self.contact is None:
            # A group row IS its name, and nothing below applies to one:
            # no avatar, no badge, no presence, no second line. Returning
            # says that outright.
            #
            # It used to fall through and be stopped further down by
            # `self.contact.avatar.icon` raising AttributeError on None,
            # inside a blanket except that swallowed it -- so the early
            # return was really an exception, and the moment that except
            # was narrowed the row carried on into drawFirstLine and drew
            # the group's name a second time, 15pt right and 4pt down.
            self.drawGroup()
            return

        # The badge takes no width from the text lines, but drawFirstLine
        # and drawSecondLine read this before it is drawn, so it is set
        # here rather than left to the draw itself.
        self.badgeWidth = 0.0

        self.contentRightEdge = None
        self.timeLeftEdge = None
        self.timeSize = None
        try:
            self.drawLastMessageTime()
        except Exception as e:
            BlinkLogger().log_error('Cannot draw the last message time: %s' % e)

        try:
            self.drawAvatar()
        except Exception as e:
            BlinkLogger().log_error('Cannot draw the contact avatar: %s' % e)

        # AFTER the avatar. The badge sits on the photo's corner, and
        # drawing it first -- which it was, to get at the frame before
        # drawFirstLine mutates it -- simply put it underneath, where a
        # contact with a picture showed no badge at all and one without
        # showed it fine. The frame is still untouched here: only
        # drawFirstLine and drawSecondLine change it, and they run below.
        #
        # Its own try/except, because the blanket one silently blanks
        # whatever follows it in the same block.
        try:
            self.drawUnreadBadge()
        except Exception as e:
            BlinkLogger().log_error('Cannot draw unread badge: %s' % e)

        try:
            self.drawActiveMedia()
            self.drawFirstLine()
            self.drawSecondLine()
            self.drawPresenceIcon()
        except Exception:
            pass

    @objc.python_method
    def textWidthFrom(self, frame, origin_x, trailing):
        """How wide a text line may be before it would run into the time."""
        limit = self.contentRightEdge
        if limit is not None:
            return max(20.0, limit - origin_x)
        return frame.size.width - trailing - getattr(self, 'badgeWidth', 0.0)

    @objc.python_method
    def drawLastMessageTime(self):
        """The last message's moment, at the right of the row.

        Drawn from the untouched frame, before drawFirstLine and
        drawSecondLine mutate it, and it publishes contentRightEdge so the
        name and detail truncate before the time rather than under it.
        """
        if self.contact is None:
            return
        text = format_last_message_time(self.lastMessageTime)
        if not text:
            return
        attrs = self.timeAttributes_highlighted if self.isHighlighted() else self.timeAttributes
        string = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        width = string.size().width

        # The cell frame is the COLUMN, which does not always keep up with
        # the outline it lives in -- and a column narrower than the view puts
        # a "right-aligned" stamp in the middle of the row. Rows span the
        # whole table, so measure against the view when it is the wider of
        # the two.
        right_edge = self.frame.origin.x + self.frame.size.width
        try:
            visible = self.view.bounds().size.width
            if visible > right_edge:
                right_edge = visible
        except Exception:
            pass

        self.timeLeftEdge = right_edge - width - self.TIME_RIGHT_GUTTER
        self.timeSize = string.size()
        self.timeColor = (NSColor.whiteColor() if self.isHighlighted()
                          else NSColor.secondaryLabelColor())
        string.drawAtPoint_((self.timeLeftEdge, self.frame.origin.y + 7))
        # Where the name and detail have to stop. An absolute x rather than
        # an inset, because the two are measured from different origins once
        # the column and the view disagree about how wide the row is.
        self.contentRightEdge = self.timeLeftEdge - 4.0

    @objc.python_method
    def drawGroup(self):
        #return objc.super(ContactCell, self).drawWithFrame_inView_(frame, view)
        frame = self.frame
        frame.origin.x = 20
        frame.origin.y += 2
        rect = NSMakeRect(frame.origin.x, frame.origin.y, frame.size.width, frame.size.height)
        self.stringValue().drawInRect_withAttributes_(rect, self.groupAttributes)

    @objc.python_method
    def drawFirstLine(self):
        frame = self.frame
        frame.origin.x = self.TEXT_LEFT
        frame.origin.y += 6

        rect = NSMakeRect(frame.origin.x, frame.origin.y,
                          self.textWidthFrom(frame, frame.origin.x, 10),
                          frame.size.height)
        attrs = self.firstLineAttributes if not self.isHighlighted() else self.firstLineAttributes_highlighted
        self.stringValue().drawInRect_withAttributes_(rect, attrs)

    @objc.python_method
    def drawSecondLine(self):
        frame = self.frame
        frame.origin.y += 16
        if self.composing:
            # The detail line's own attributes, not a set of its own: they
            # are the ones already proven to draw on this row, in this
            # appearance, at this size. A separate dictionary is one more
            # thing that can come back empty -- and an empty attributes
            # dictionary draws in default black, which on a dark row is
            # indistinguishable from drawing nothing at all.
            #
            # Takes the line whether or not the contact has a detail: a
            # row whose detail happens to be empty is still a row someone
            # is typing at.
            text = COMPOSING_TEXT
            attrs = self.secondLineAttributes if not self.isHighlighted() else self.secondLineAttributes_highlighted
        elif self.contact.detail:
            text = self.contact.detail
            attrs = self.secondLineAttributes if not self.isHighlighted() else self.secondLineAttributes_highlighted
        else:
            return

        rect = NSMakeRect(frame.origin.x, frame.origin.y,
                          self.textWidthFrom(frame, frame.origin.x, 25),
                          frame.size.height)
        text.drawInRect_withAttributes_(rect, attrs)

    @objc.python_method
    def drawUnreadBadge(self):
        """Unread count as a badge on the contact's avatar, like mobile.

        Sits on the avatar's top-right corner with a ring in the row colour
        so it stays legible over a photo. Because it lives on the avatar it
        does not steal width from the name or detail lines.
        """
        count = getattr(self, 'unreadCount', 0)
        if not count or self.contact is None:
            return

        text = '99+' if count > 99 else str(count)
        highlighted = self.isHighlighted()
        fill = NSColor.whiteColor() if highlighted else NSColor.systemRedColor()
        ink = NSColor.systemRedColor() if highlighted else NSColor.whiteColor()

        attrs = NSDictionary.dictionaryWithObjectsAndKeys_(
            NSFont.boldSystemFontOfSize_(self.BADGE_FONT_SIZE), NSFontAttributeName,
            ink, NSForegroundColorAttributeName)
        label = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        size = label.size()

        avatar_right = self.AVATAR_LEFT + self.AVATAR_SIZE
        avatar_top = self.frame.origin.y + self.AVATAR_TOP

        height = self.BADGE_HEIGHT
        width = max(height, size.width + 6.0)
        # Right edge just short of where the name starts, whatever the count
        # widens the badge to, and high enough to read as sitting on top of
        # the avatar rather than inside it.
        rect = NSMakeRect(avatar_right - width + 4.0, avatar_top - 2.0, width, height)

        badge = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, height / 2.0, height / 2.0)
        # ring, so the badge reads against a busy photo
        (NSColor.alternateSelectedControlColor() if highlighted
         else NSColor.controlBackgroundColor()).set()
        badge.setLineWidth_(2.0)
        badge.stroke()
        fill.set()
        badge.fill()

        label.drawAtPoint_((rect.origin.x + (width - size.width) / 2.0,
                            rect.origin.y + (height - size.height) / 2.0))

        # on the avatar, so it costs the text lines nothing
        self.badgeWidth = 0.0

    @objc.python_method
    def drawActiveMedia(self):
        if type(self.contact) not in (BlinkConferenceContact, BlinkMyselfConferenceContact):
            return

        padding = 16
        left = self.frame.size.width - 8
        if 'audio-onhold' in self.contact.active_media:
            left = left - padding
            self.drawIcon(self.audioHoldIcon, left, self.frame.origin.y +14, 16, 16)
        elif 'audio' in self.contact.active_media:
            left = left - padding
            self.drawIcon(self.audioIcon, left, self.frame.origin.y +14, 16, 16)

        if 'message' in self.contact.active_media:
            left = left - padding
            self.drawIcon(self.chatIcon, left, self.frame.origin.y +14, 16, 16)

        if 'screen' in self.contact.active_media:
            left = left - padding - 2
            self.drawIcon(self.screenIcon, left, self.frame.origin.y +14, 16, 16)

    @objc.python_method
    def drawPresenceIcon(self):
        status = 'offline'
        if type(self.contact) is BlinkMyselfConferenceContact:
            account = self.contact.account
            if account.enabled and account.presence.enabled:
                settings = SIPSimpleSettings()
                status = settings.presence_state.status.lower()
        elif type(self.contact) is BlinkConferenceContact:
            blink_contact = self.contact.presence_contact
            if not isinstance(blink_contact, BlinkPresenceContact):
                return
            if not blink_contact.contact.presence.subscribe:
                return
            status = presence_status_for_contact(blink_contact)
        elif type(self.contact) is BlinkHistoryViewerContact:
            blink_contact = self.contact.presence_contact
            if not isinstance(blink_contact, BlinkPresenceContact):
                return
            if not blink_contact.contact.presence.subscribe:
                return
            status = presence_status_for_contact(blink_contact)

        elif type(self.contact) is HistoryBlinkContact:
            blink_contact = self.contact.contact
            if not isinstance(blink_contact, BlinkPresenceContact):
                return
            if not blink_contact.contact.presence.subscribe:
                return
            status = presence_status_for_contact(blink_contact)
        elif isinstance(self.contact, BlinkPresenceContact):
            blink_contact = self.contact
            if not blink_contact.contact.presence.subscribe:
                return
            status = presence_status_for_contact(blink_contact)
        elif type(self.contact) is BonjourBlinkContact:
            account = BonjourAccount()
            if not account.presence.enabled:
                return
            blink_contact = self.contact
            status = presence_status_for_contact(blink_contact)
        elif type(self.contact) is SystemAddressBookBlinkContact:
            return
        elif type(self.contact) is LdapSearchResultContact:
            return
        elif type(self.contact) is SearchResultContact:
            return

        if not status:
            return
        try:
            icon = presence_status_icons[status]
        except KeyError:
            pass

        has_locations = None
        if isinstance(self.contact, (BlinkOnlineContact, BlinkPresenceContact)):
            try:
                has_locations = any(device['location'] for device in list(self.contact.presence_state['devices'].values()) if device['location'] is not None)
            except KeyError:
                pass

        frame = self.frame
        frame.origin.y -= 22
        #if has_locations:
        #    left = self.view.frame().size.width - 22
        #    self.drawIcon(self.locationIcon, left, self.frame.origin.y +14, 16, 16)

        # presence bar
        frame.size.width = 5
        if type(self.contact) in (BlinkConferenceContact, BlinkMyselfConferenceContact):
            frame.size.height = 14
            frame.origin.y += 15
        frame.origin.x = self.view.frame().size.width - 6

        rect = NSInsetRect(frame, 0, 0)

        if status == 'available':
            NSColor.greenColor().set()
        elif status == 'away':
            NSColor.yellowColor().set()
        elif status == 'busy':
            NSColor.redColor().set()
        else:
            NSColor.whiteColor().set()

        border = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 2.0, 2.0)
        border.setLineWidth_(0.08)
        border.fill()
        NSColor.blackColor().set()
        border.stroke()

        # sleep icon
        if isinstance(self.contact, (BlinkOnlineContact, BlinkPresenceContact)):
            if self.contact.presence_state['time_offset'] is not None:
                ctime = datetime.datetime.utcnow() + self.contact.presence_state['time_offset']
                hour = int(ctime.strftime("%H"))
                if hour > 21 or hour < 7:
                    self.drawNightIcon()

    @objc.python_method
    def drawNightIcon(self):
        """The moon, immediately before the time and the same height as it.

        Sized and tinted to the text rather than drawn as a 16pt icon: it
        reads as a prefix to the time -- "it is night there, at 21:40" -- and
        the plain white artwork is invisible on a light row.
        """
        if self.timeLeftEdge is None or self.timeSize is None:
            # no time on this row: fall back to the old fixed position
            left = self.view.frame().size.width - 26
            self.drawIcon(self.nightIcon, left, self.frame.origin.y + 14, 16, 16)
            return

        size = max(10.0, round(self.timeSize.height) - 2.0)
        gap = 4.0
        left = self.timeLeftEdge - size - gap
        # centre it on the time's own line rather than the row
        top = self.frame.origin.y + 7 + (self.timeSize.height - size) / 2.0
        self.drawIcon(self.tintedNightIcon(), left, top, size, size)

    @objc.python_method
    def tintedNightIcon(self):
        """The moon in the time's colour, cached per colour."""
        colour = self.timeColor or NSColor.secondaryLabelColor()
        key = str(colour)
        cached = ContactCell._night_icon_cache.get(key)
        if cached is not None:
            return cached
        try:
            source = self.nightIcon
            tinted = source.copy()
            tinted.lockFocus()
            colour.set()
            NSRectFillUsingOperation(
                NSMakeRect(0, 0, tinted.size().width, tinted.size().height),
                NSCompositeSourceAtop)
            tinted.unlockFocus()
        except Exception as e:
            BlinkLogger().log_error('Cannot tint the night icon: %s' % e)
            tinted = self.nightIcon
        ContactCell._night_icon_cache[key] = tinted
        return tinted

    @objc.python_method
    def avatarName(self):
        """What the initials and the colour are derived from."""
        contact = self.contact
        name = getattr(contact, 'name', None)
        if name:
            return str(name)
        return str(getattr(contact, 'uri', '') or '')

    @objc.python_method
    def drawAvatar(self):
        """The contact as a circle: their photograph, or their initials.

        The same treatment as the message bubbles and as mobile. A contact
        with no photograph used to get default_user_icon.tiff -- one grey
        person glyph shared by everybody, which is no help at all in a list
        where telling one row from another is the whole point. That file is
        a real image and loads like any other, so "has a photograph" is a
        question about its name, which Avatars answers.

        The three stand-ins that mean something -- a room, a watcher waiting
        to be allowed, somebody refused -- are drawn exactly as they were,
        whole and unclipped. A room is not a person, and the initials of its
        name say less than the group glyph does; and these are line drawings
        that would lose their outer strokes to a circle, with nothing
        outside the frame to give up in exchange the way a photograph has.
        """
        avatar = self.contact.avatar
        icon = avatar.icon if avatar is not None else None
        filename = os.path.basename(str(getattr(avatar, 'path', None) or ''))
        top = self.frame.origin.y + self.AVATAR_TOP
        if filename in GLYPH_AVATARS:
            if icon is not None:
                self.drawIcon(icon, self.AVATAR_LEFT, top,
                              self.AVATAR_SIZE, self.AVATAR_SIZE)
            return
        if filename == NO_PHOTO_AVATAR:
            icon = None
        draw_avatar(NSMakeRect(self.AVATAR_LEFT, top,
                               self.AVATAR_SIZE, self.AVATAR_SIZE),
                    icon, self.avatarName())

    @objc.python_method
    def drawIcon(self, icon, origin_x, origin_y, size_x, size_y):
        size = icon.size()
        if not size or not size.height:
            return
        rect = NSMakeRect(0, 0, size.width, size.height)
        trect = NSMakeRect(origin_x, origin_y, (size_y/size.height) * size.width, size_x)
        icon.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(trect, rect, NSCompositeSourceOver, 1.0, True, None)

