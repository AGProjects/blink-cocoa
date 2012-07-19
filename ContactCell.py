# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *


class ContactCell(NSTextFieldCell):
    contact = None
    view = None
    frame = None

    audioIcon = NSImage.imageNamed_("audio_16")
    audioHoldIcon = NSImage.imageNamed_("paused_16")
    chatIcon = NSImage.imageNamed_("pencil")
    screenIcon = NSImage.imageNamed_("display_16")

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

    def drawFirstLine(self):
        frame = self.frame
        frame.origin.x = 35
        frame.origin.y += 2

        rect = NSMakeRect(frame.origin.x, frame.origin.y, frame.size.width-10, frame.size.height)
        attrs = self.firstLineAttributes if not self.isHighlighted() else self.firstLineAttributes_highlighted
        self.stringValue().drawInRect_withAttributes_(rect, attrs)

    def drawSecondLine(self):
        if self.contact.detail:
            frame = self.frame
            frame.origin.y += 15
            rect = NSMakeRect(frame.origin.x, frame.origin.y, frame.size.width-10, frame.size.height)
            attrs = self.secondLineAttributes if not self.isHighlighted() else self.secondLineAttributes_highlighted
            self.contact.detail.drawInRect_withAttributes_(rect, attrs)

    def drawActiveMedia(self):
        if not hasattr(self.contact, "active_media"):
            return

        padding = 16
        left = self.frame.size.width
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

    def drawPresenceIndicator(self):
        if not hasattr(self.contact, "presence_indicator") or self.contact.presence_indicator is None:
            return

        indicator_width = 5
        frame = self.frame
        frame.size.width = indicator_width
        frame.origin.x = self.view.frame().size.width - indicator_width
        frame.origin.y -= 17

        rect = NSInsetRect(frame, 0, 0)

        if self.contact.presence_indicator == 'available':
            NSColor.greenColor().set()
        elif self.contact.presence_indicator == 'activity':
            NSColor.yellowColor().set()
        elif self.contact.presence_indicator == 'busy':
            NSColor.redColor().set()
        else:
            NSColor.whiteColor().set()

        border = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 2.0, 2.0)
        border.setLineWidth_(0.1)
        border.fill()
        NSColor.blackColor().set()
        border.stroke()

    def drawIcon(self, icon, origin_x, origin_y, size_x, size_y):
        size = icon.size()
        if not size or not size.height:
            return
        rect = NSMakeRect(0, 0, size.width, size.height)
        trect = NSMakeRect(origin_x, origin_y, (size_y/size.height) * size.width, size_x)
        if icon.respondsToSelector_("drawInRect:fromRect:operation:fraction:respectFlipped:hints:"):
            # New API in Snow Leopard to correctly draw an icon in context respecting its flipped attribute
            icon.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(trect, rect, NSCompositeSourceOver, 1.0, True, None)
        else:
            # Leopard, see http://developer.apple.com/mac/library/releasenotes/cocoa/AppKit.html
            icon_flipped = icon.copy()
            icon_flipped.setFlipped_(True)
            icon_flipped.drawInRect_fromRect_operation_fraction_(trect, rect, NSCompositeSourceOver, 1.0)


class WatcherContactCell(ContactCell):
    pass


