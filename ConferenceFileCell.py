# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *


class ConferenceFileCell(NSTextFieldCell):
    conference_file = None

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

    def drawingRectForBounds_(self, rect):
        return rect

    def cellSize(self):
        if self.conference_file is None:
            return super(ConferenceFileCell, self).cellSize()
        return NSMakeSize(100, 30)

    def drawWithFrame_inView_(self, frame, view):
        if self.conference_file is None:
            tmp = frame
            return super(ConferenceFileCell, self).drawWithFrame_inView_(tmp, view)

        self.drawIcon(self.conference_file.icon, 2, frame.origin.y+3, 28, 28)

        # 1st line: file name
        frame.origin.x = 35
        frame.origin.y += 2
        attrs = self.nameAttrs if not self.isHighlighted() else self.nameAttrs_highlighted
        self.conference_file.name.drawAtPoint_withAttributes_(frame.origin, attrs)

        # 2nd line: file sender
        point = frame.origin
        point.y += 15
        attrs = self.infoAttrs if not self.isHighlighted() else self.infoAttrs_highlighted
        self.conference_file.sender.drawAtPoint_withAttributes_(point, attrs)

    def drawIcon(self, icon, origin_x, origin_y, size_x, size_y):
        size = icon.size()
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

