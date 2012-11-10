# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *
from util import allocate_autorelease_pool

from ContactListModel import status_icon_for_contact, BlinkPresenceContact


class ContactCell(NSTextFieldCell):
    contact = None
    view = None
    frame = None

    audioIcon = NSImage.imageNamed_("audio_16")
    audioHoldIcon = NSImage.imageNamed_("paused_16")
    chatIcon = NSImage.imageNamed_("pencil")
    screenIcon = NSImage.imageNamed_("display_16")
    locationIcon = NSImage.imageNamed_("location")
    blockedIcon = NSImage.imageNamed_("blocked")
    awayIcon = NSImage.imageNamed_("away")
    busyIcon = NSImage.imageNamed_("busy")
    availableIcon = NSImage.imageNamed_("available")
    offlineIcon = NSImage.imageNamed_("offline")

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
        self.drawPresenceIndicator()

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
            rect = NSMakeRect(frame.origin.x, frame.origin.y, frame.size.width-10, frame.size.height)
            attrs = self.secondLineAttributes if not self.isHighlighted() else self.secondLineAttributes_highlighted
            self.contact.detail.drawInRect_withAttributes_(rect, attrs)

    @allocate_autorelease_pool
    def drawActiveMedia(self):
        if not hasattr(self.contact, "active_media"):
            return

        padding = 16
        left = self.frame.size.width - 6
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
    def drawPresenceIndicator(self):
        image = '%sIcon' % status_icon_for_contact(self.contact)
        
        if image and hasattr(self, image):
            icon =  getattr(self, image)
            icon.setScalesWhenResized_(True)
            icon.setSize_(NSMakeSize(12,12))
            self.drawIcon(icon, 20, self.frame.origin.y + 5, 12, 12)        

        if not hasattr(self.contact, "presence_indicator") or self.contact.presence_indicator is None:
            return

        if isinstance(self.contact, BlinkPresenceContact):
            indicator_width = 5
            frame = self.frame
            frame.size.width = indicator_width
            frame.origin.x = self.view.frame().size.width - indicator_width
            frame.origin.y -= 17

            rect = NSInsetRect(frame, 0, 0)

            if self.contact.presence_indicator == 'available':
                NSColor.greenColor().set()
            elif self.contact.presence_indicator == 'away':
                NSColor.yellowColor().set()
            elif self.contact.presence_indicator == 'busy':
                NSColor.redColor().set()
            else:
                NSColor.whiteColor().set()

            border = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 2.0, 2.0)
            border.setLineWidth_(0.2)
            border.fill()
            NSColor.blackColor().set()
            border.stroke()

        if not hasattr(self.contact, "presence_state"):
            return

        try:
            has_locations = any(device['location'] for device in self.contact.presence_state['devices'].values() if device['location'] is not None)
        except KeyError:
            return

        if has_locations:
            left = self.view.frame().size.width - 20
            self.drawIcon(self.locationIcon, left, self.frame.origin.y +14, 16, 16)

    @allocate_autorelease_pool
    def drawIcon(self, icon, origin_x, origin_y, size_x, size_y):
        size = icon.size()
        if not size or not size.height:
            return
        rect = NSMakeRect(0, 0, size.width, size.height)
        trect = NSMakeRect(origin_x, origin_y, (size_y/size.height) * size.width, size_x)
        icon.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(trect, rect, NSCompositeSourceOver, 1.0, True, None)



