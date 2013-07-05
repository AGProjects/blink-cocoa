# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSCompositeSourceOver,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSLineBreakByTruncatingTail,
                    NSParagraphStyleAttributeName)
from Foundation import (NSBezierPath,
                        NSColor,
                        NSDictionary,
                        NSFont,
                        NSImage,
                        NSInsetRect,
                        NSMakeRect,
                        NSMakeSize,
                        NSParagraphStyle,
                        NSTextFieldCell)
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.account import BonjourAccount

from ContactListModel import presence_status_for_contact, presence_status_icons, BonjourBlinkContact, BlinkPresenceContact, BlinkMyselfConferenceContact,BlinkConferenceContact, HistoryBlinkContact, SystemAddressBookBlinkContact, LdapSearchResultContact, SearchResultContact
from util import allocate_autorelease_pool


class ContactCell(NSTextFieldCell):
    contact = None
    view = None
    frame = None

    audioIcon = NSImage.imageNamed_("audio_16")
    audioHoldIcon = NSImage.imageNamed_("paused_16")
    chatIcon = NSImage.imageNamed_("pencil")
    screenIcon = NSImage.imageNamed_("display_16")
    locationIcon = NSImage.imageNamed_("location")

    style = NSParagraphStyle.defaultParagraphStyle().mutableCopy()
    style.setLineBreakMode_(NSLineBreakByTruncatingTail)
    firstLineAttributes = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(12.0), NSFontAttributeName, style, NSParagraphStyleAttributeName)
    firstLineAttributes_highlighted = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(12.0), NSFontAttributeName, NSColor.whiteColor(), NSForegroundColorAttributeName, style, NSParagraphStyleAttributeName)
    secondLineAttributes = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(NSFont.labelFontSize()-1), NSFontAttributeName, NSColor.grayColor(), NSForegroundColorAttributeName, style, NSParagraphStyleAttributeName)
    secondLineAttributes_highlighted = NSDictionary.dictionaryWithObjectsAndKeys_( NSFont.systemFontOfSize_(NSFont.labelFontSize()-1), NSFontAttributeName, NSColor.whiteColor(), NSForegroundColorAttributeName, style, NSParagraphStyleAttributeName)

    def setContact_(self, contact):
        self.contact = contact

    def setMessageIcon_(self, icon):
        self.messageIcon = icon

    def drawingRectForBounds_(self, rect):
        return rect

    def cellSize(self):
        if self.contact is None:
            return super(ContactCell, self).cellSize()
        return NSMakeSize(100, 30)

    @allocate_autorelease_pool
    def drawWithFrame_inView_(self, frame, view):
        if self.contact is None:
            return super(ContactCell, self).drawWithFrame_inView_(frame, view)

        self.frame = frame
        self.view = view

        icon = self.contact.avatar.icon
        self.drawIcon(icon, 2, self.frame.origin.y+3, 28, 28)

        self.drawActiveMedia()
        self.drawFirstLine()
        self.drawSecondLine()
        self.drawPresenceIcon()

    @allocate_autorelease_pool
    def drawFirstLine(self):
        frame = self.frame
        frame.origin.x = 35
        frame.origin.y += 2

        rect = NSMakeRect(frame.origin.x, frame.origin.y, frame.size.width-10, frame.size.height)
        attrs = self.firstLineAttributes if not self.isHighlighted() else self.firstLineAttributes_highlighted
        self.stringValue().drawInRect_withAttributes_(rect, attrs)

    @allocate_autorelease_pool
    def drawSecondLine(self):
        frame = self.frame
        frame.origin.y += 15
        if self.contact.detail:
            rect = NSMakeRect(frame.origin.x, frame.origin.y, frame.size.width - 25, frame.size.height)
            attrs = self.secondLineAttributes if not self.isHighlighted() else self.secondLineAttributes_highlighted
            self.contact.detail.drawInRect_withAttributes_(rect, attrs)

    @allocate_autorelease_pool
    def drawActiveMedia(self):
        if not hasattr(self.contact, "active_media"):
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

    @allocate_autorelease_pool
    def drawPresenceIcon(self):
        status = 'offline'
        if isinstance(self.contact, BlinkMyselfConferenceContact):
            account = self.contact.account
            if account.enabled and account.presence.enabled:
                settings = SIPSimpleSettings()
                status = settings.presence_state.status.lower()
        elif isinstance(self.contact, BlinkConferenceContact):
            blink_contact = self.contact.presence_contact
            if not isinstance(blink_contact, BlinkPresenceContact):
                return
            if not blink_contact.contact.presence.subscribe:
                return
            status = presence_status_for_contact(blink_contact, self.contact.uri)
        elif isinstance(self.contact, HistoryBlinkContact):
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
        elif isinstance(self.contact, BonjourBlinkContact):
            account = BonjourAccount()
            if not account.presence.enabled:
                return
            blink_contact = self.contact
            status = presence_status_for_contact(blink_contact)
        elif isinstance(self.contact, SystemAddressBookBlinkContact):
            return
        elif isinstance(self.contact, LdapSearchResultContact):
            return
        elif isinstance(self.contact, SearchResultContact):
            return

        if not status:
            return
        try:
            icon = presence_status_icons[status]
        except KeyError:
            pass

        has_locations = None
        if isinstance(self.contact, BlinkPresenceContact):
            try:
                has_locations = any(device['location'] for device in self.contact.presence_state['devices'].values() if device['location'] is not None)
            except KeyError:
                pass

        frame = self.frame
        frame.origin.y -= 17
        if has_locations:
            left = self.view.frame().size.width - 22
            self.drawIcon(self.locationIcon, left, self.frame.origin.y +14, 16, 16)

        # presence bar
        frame.size.width = 5
        if hasattr(self.contact, "active_media"):
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

    @allocate_autorelease_pool
    def drawIcon(self, icon, origin_x, origin_y, size_x, size_y):
        size = icon.size()
        if not size or not size.height:
            return
        rect = NSMakeRect(0, 0, size.width, size.height)
        trect = NSMakeRect(origin_x, origin_y, (size_y/size.height) * size.width, size_x)
        icon.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(trect, rect, NSCompositeSourceOver, 1.0, True, None)



