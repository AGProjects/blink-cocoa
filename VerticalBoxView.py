# Copyright (C) 2009 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *


class VerticalBoxView(NSView):
    def initWithFrame_(self, frame):
        self = super(VerticalBoxView, self).initWithFrame_(frame)
        if self:
            self.backgroundColor = None
            self.spacing = 0
            self.border = 0
        return self

    def setSpacing_(self, spacing):
        self.spacing = spacing
    
    def setBorderWidth_(self, border):
        self.border = border

    def didAddSubview_(self, subview):
        minimumHeight = self.spacing * (self.subviews().count()-1) + self.border*2
        for view in self.subviews():
            if hasattr(view, "expand"):
                expandCount += 1
            else:
                minimumHeight += NSHeight(view.frame()) 
        self.relayout()

    def isFlipped(self):
        return True

    def setBackgroundColor_(self, color):
        self.backgroundColor = color
        self.setNeedsDisplay_(True)
    
    def drawRect_(self, rect):
        if self.backgroundColor:
            self.backgroundColor.set()
            NSRectFill(rect)

    def minimumHeight(self):
        h = self.spacing * (self.subviews().count()-1) + 2 * self.border
        for view in self.subviews():
            h += NSHeight(view.frame())
        return h
    
    def relayout(self):
        self.resizeWithOldSuperviewSize_(NSZeroSize)

    def resizeWithOldSuperviewSize_(self, oldSize):
        sview = self.enclosingScrollView()
        frame = self.frame()
        
        if sview:
            width = sview.contentSize().width
        else:
            width = NSWidth(frame) - 2 * self.border

        expandCount = 0
        minimumHeight = self.minimumHeight()
        for view in self.subviews():
            if hasattr(view, "expand"):
                expandCount += 1

        expandedHeight= 0
        if expandCount > 0:
            expandedHeight= (NSHeight(frame) - minimumHeight) / expandedCount

        y = self.border
        for view in self.subviews():
            rect = view.frame()
            rect.origin.x = self.border
            rect.size.width = width
            rect.origin.y = y
            if hasattr(view, "expand"):
                rect.size.height = expandedHeight

            view.setFrame_(rect)
            y += NSHeight(rect) + self.spacing

        if sview:
            frame.size.width = sview.contentSize().width
        frame.size.height = minimumHeight
        self.setFrame_(frame)


