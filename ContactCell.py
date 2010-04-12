# Copyright (C) 2009 AG Projects. See LICENSE for details.     
#

from Foundation import *
from AppKit import *


class ContactCell(NSTextFieldCell):
    contact = None
    
    nameAttrs = NSDictionary.dictionaryWithObjectsAndKeys_(
      NSFont.systemFontOfSize_(12.0), NSFontAttributeName)

    infoAttrs = NSDictionary.dictionaryWithObjectsAndKeys_(
      NSFont.systemFontOfSize_(NSFont.labelFontSize()-1), NSFontAttributeName,
      NSColor.grayColor(), NSForegroundColorAttributeName)

    defaultIcon = None
    
    messageIcon = None
    
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
            size = icon.size()
            rect = NSMakeRect(0, 0, size.width, size.height)
            if size.width > size.height:
                trect = NSMakeRect(2, frame.origin.y + 3, 28, (28/size.width) * size.height)
            else:
                trect = NSMakeRect(2, frame.origin.y + 3, (28/size.height) * size.width, 28)
            if icon.respondsToSelector_("drawInRect:fromRect:operation:fraction:respectFlipped:hints:"):
                # new API in snow leopard to correctly draw an icon in context respecting its flipped attribute
                icon.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(trect, rect, NSCompositeSourceOver, 1.0, True, None)
            else:
                # draw icon for Leopard, see http://developer.apple.com/mac/library/releasenotes/cocoa/AppKit.html
                icon_flipped = icon.copy()
                icon_flipped.setFlipped_(True)
                icon_flipped.drawInRect_fromRect_operation_fraction_(trect, rect, NSCompositeSourceOver, 1.0)

        frame.origin.x = 35
        frame.origin.y += 2
        self.stringValue().drawAtPoint_withAttributes_(frame.origin, self.nameAttrs)

        point = frame.origin
        point.y += 15

        if self.contact.detail:
            self.contact.detail.drawAtPoint_withAttributes_(point, self.infoAttrs)
