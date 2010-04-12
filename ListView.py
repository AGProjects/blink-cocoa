# Copyright (C) 2009 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *
from VerticalBoxView import VerticalBoxView


class ListView(VerticalBoxView):
    allowMultiSelection = False
    allowSelection = True
    alternateRows = True
    selection = -1

    def initWithFrame_(self, frame):
        self = super(ListView, self).initWithFrame_(frame)
        if self:
            self.setBackgroundColor_(NSColor.whiteColor())
            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "windowChangedKey:", NSWindowDidBecomeKeyNotification, None)
            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "windowChangedKey:", NSWindowDidResignKeyNotification, None)
        return self

    def dealloc(self):
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        VerticalBoxView.dealloc(self)

    def windowChangedKey_(self, notification):
        if self.window() == notification.object():
            self.setNeedsDisplay_(True)

    def numberOfItems(self):
        return self.subviews().count()

    def acceptsFirstResponder(self):
        return True        

    def drawRect_(self, rect):
        VerticalBoxView.drawRect_(self, rect)
        
        if self.alternateRows:
            i = 0
            NSColor.colorWithCalibratedRed_green_blue_alpha_(237/256.0, 243/256.0, 254/256.0, 1.0).set()
            for v in self.subviews():
                if i % 2 == 1:
                    NSRectFill(v.frame())
                i += 1
        
        if self.selection != -1 and self.selection < self.subviews().count():
            if self.window().isKeyWindow():
                NSColor.alternateSelectedControlColor().set()
            else:
                NSColor.lightGrayColor().set()
            NSRectFill(self.subviews()[self.selection].frame())

    def minimumHeight(self):
        return max(VerticalBoxView.minimumHeight(self), 1)#NSHeight(self.enclosingScrollView().documentVisibleRect()))

    def insertItemView_before_(self, view, before):

        # check if its already in
        for child in self.subviews():
            if child == view:
                return

        frame = view.frame()
        frame.origin.y = 0
        frame.size.width = NSWidth(self.frame())
        view.setFrame_(frame)
        if before is None:
            self.addSubview_(view)
        else:
            self.addSubview_positioned_relativeTo_(view, NSWindowBelow, before)

        frame = self.frame()
        frame.size.height = self.minimumHeight()
        self.setFrame_(frame)

        self.relayout()


    def addItemView_(self, view):
        self.insertItemView_before_(view, None)

    def mouseDown_(self, event):
        pos = self.convertPointFromBase_(event.locationInWindow())
        row = -1
        for item in self.subviews():
            row += 1
            if pos.y < NSMaxY(item.frame()):
                self.setSelectedRow_(row)
                return
        self.setSelectedRow_(-1)

    def resizeSubviewsWithOldSize_(self, oldSize):
        self.relayout()

    def setSelectedRow_(self, row):
        subviews = self.subviews()
        if self.selection != -1:
            if self.selection < subviews.count():
                sv = subviews[self.selection]
                if sv.respondsToSelector_("setSelected:"):
                    sv.setSelected_(False)
        self.selection = row
        if row != -1:
            sv = subviews[row]
            if sv.respondsToSelector_("setSelected:"):
                sv.setSelected_(True)
        self.setNeedsDisplay_(True)

    def removeItemView_(self, view):
        view.removeFromSuperview()

        frame = self.frame()
        frame.size.height = self.minimumHeight()
        self.setFrame_(frame)

        self.relayout()


