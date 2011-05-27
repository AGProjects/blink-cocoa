# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *


class ContactCell(NSTextFieldCell):
    contact = None

    nameAttrs = NSDictionary.dictionaryWithObjectsAndKeys_(
      NSFont.systemFontOfSize_(12.0), NSFontAttributeName)

    nameAttrs_highlighted = NSDictionary.dictionaryWithObjectsAndKeys_(
      NSFont.systemFontOfSize_(12.0), NSFontAttributeName,
      NSColor.whiteColor(), NSForegroundColorAttributeName)

    infoAttrs = NSDictionary.dictionaryWithObjectsAndKeys_(
      NSFont.systemFontOfSize_(NSFont.labelFontSize()-1), NSFontAttributeName,
      NSColor.grayColor(), NSForegroundColorAttributeName)

    infoAttrs_highlighted = NSDictionary.dictionaryWithObjectsAndKeys_(
      NSFont.systemFontOfSize_(NSFont.labelFontSize()-1), NSFontAttributeName,
      NSColor.whiteColor(), NSForegroundColorAttributeName)

    defaultIcon = None
    audioIcon = NSImage.imageNamed_("audio_16")
    audioHoldIcon = NSImage.imageNamed_("paused_16")
    chatIcon = NSImage.imageNamed_("pencil")

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
            tmp = frame
            return super(ContactCell, self).drawWithFrame_inView_(tmp, view)

        if self.defaultIcon is None:
            self.defaultIcon = NSImage.imageNamed_("NSUser")

        icon = self.contact.icon or self.defaultIcon
        if icon:
            self.drawIcon(icon, 2, frame.origin.y+3, 28, 28)

        # Align media icons to the right of the frame
        if 'message' in self.contact.active_media and ('audio' in self.contact.active_media or 'audio-onhold' in self.contact.active_media):
            self.drawIcon(self.chatIcon,  frame.size.width-32, frame.origin.y +14, 16, 16)
            if 'audio-onhold' in self.contact.active_media:
                self.drawIcon(self.audioHoldIcon, frame.size.width-16, frame.origin.y +14, 16, 16)
            else:
                self.drawIcon(self.audioIcon, frame.size.width-16, frame.origin.y +14, 16, 16)
        elif 'message' in self.contact.active_media:
            self.drawIcon(self.chatIcon,  frame.size.width-16, frame.origin.y +14, 16, 16)
        elif 'audio' in self.contact.active_media:
            self.drawIcon(self.audioIcon, frame.size.width-16, frame.origin.y +14, 16, 16)
        elif 'audio-onhold' in self.contact.active_media:
            self.drawIcon(self.audioHoldIcon, frame.size.width-16, frame.origin.y +14, 16, 16)

        # Print Display Name 1st line
        frame.origin.x = 35
        frame.origin.y += 2
        attrs = self.nameAttrs if not self.isHighlighted() else self.nameAttrs_highlighted
        self.stringValue().drawAtPoint_withAttributes_(frame.origin, attrs)

        # Print Detail 2nd line
        if self.contact.detail:
            point = frame.origin
            point.y += 15
            attrs = self.infoAttrs if not self.isHighlighted() else self.infoAttrs_highlighted
            self.contact.detail.drawAtPoint_withAttributes_(point, attrs)

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
