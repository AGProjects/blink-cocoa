# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

from Foundation import *
from AppKit import *

class HorizontalBoxView(NSView):
    def initWithFrame_(self, frame):
        self = super(HorizontalBoxView, self).initWithFrame_(frame)
        if self:
            self.backgroundColor = None
            self.spacing = 0
            self.border = 0
            self.expandingViews = set()
        return self

    def setViewExpands(self, view, flag = True):
        if flag and not view in self.expandingViews:
            self.expandingViews.add(view)
        elif not flag and view in self.expandingViews:
            self.expandingViews.remove(view)

    def setSpacing_(self, spacing):
        self.spacing = spacing
    
    def setBorderWidth_(self, border):
        self.border = border

    def didAddSubview_(self, subview):
        minimumWidth = self.spacing * (self.subviews().count()-1) + self.border*2
        expandCount = 0
        for view in self.subviews():
            if view in self.expandingViews:
                expandCount += 1
            else:
                minimumWidth += NSWidth(view.frame()) 

        frame = self.frame()
        if NSWidth(frame) != minimumWidth:
            frame.size.width = minimumWidth
            self.setFrame_(frame)
    
    def isFlipped(self):
        return True

    def setBackgroundColor_(self, color):
        self.backgroundColor = color
        self.setNeedsDisplay_(True)
    
    def drawRect_(self, rect):
        if self.backgroundColor:
            self.backgroundColor.set()
            NSRectFill(rect)
        #NSColor.redColor().set()
        #NSFrameRect(rect)

    def resizeSubviewsWithOldSize_(self, oldSize):
        frame = self.frame()
        height = NSHeight(frame) - 2 * self.border
        
        expandCount = 0
        minimumWidth = 2 * self.border
        for view in self.subviews():
            if view in self.expandingViews:
                expandCount += 1
            else:
                minimumWidth += NSWidth(view.frame())
            minimumWidth += self.spacing

        expandedWidth= 0
        if expandCount > 0:
            expandedWidth = int((NSWidth(frame) - minimumWidth) / expandCount)
        
        x = self.border
        for view in self.subviews():
            rect = view.frame()
            if not view.isKindOfClass_(NSTextField.class__()):
                rect.origin.y = self.border
                rect.size.height = height
            else:
                rect.origin.y = self.border + (height - NSHeight(rect)) / 2
            rect.origin.x = x
            if view in self.expandingViews:
                rect.size.width = expandedWidth
            
            view.setFrame_(rect)
            x += NSWidth(rect) + self.spacing
            

