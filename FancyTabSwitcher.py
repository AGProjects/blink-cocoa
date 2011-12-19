# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *
import objc

MIN_TAB_WIDTH = 100
TAB_WIDTH = 220

class FancyTabItem(NSView):
    switcher = None
    label = None
    badgeLabel = None
    busyIndicator = None
    closeButton = None
    mouseInside = False
    trackingArea = None
    cachedDragImage = None
    composing = False
    screen_sharing = False
    closeIcon = NSImage.imageNamed_("NSStopProgressFreestandingTemplate").copy()
    composeIcon = NSImage.imageNamed_("pencil")
    screenIcon = NSImage.imageNamed_("display_red_16")

    badgeAttributes = None
    draggedOut = False
    
    @classmethod
    def initialize(self):
        paraStyle = NSParagraphStyle.defaultParagraphStyle().mutableCopy()
        paraStyle.setAlignment_(NSCenterTextAlignment)
    
        self.badgeAttributes = NSMutableDictionary.dictionaryWithObjectsAndKeys_(NSFont.boldSystemFontOfSize_(8),
                                NSFontAttributeName, NSColor.whiteColor(), NSForegroundColorAttributeName,
                                paraStyle, NSParagraphStyleAttributeName)



    def initWithSwitcher_item_(self, switcher, item):
        self = NSView.initWithFrame_(self, NSMakeRect(0, 2, 100, 18))
        if self:
            self.closeIcon.setSize_(NSMakeSize(12, 12))
            self.closeButton = NSButton.alloc().initWithFrame_(NSMakeRect(5, 5, 12, 14))
            self.closeButton.setImagePosition_(NSImageOnly)
            self.closeButton.setButtonType_(NSMomentaryChangeButton)
            self.closeButton.cell().setBezelStyle_(NSSmallSquareBezelStyle)
            self.closeButton.setBordered_(False)
            self.closeButton.setImage_(self.closeIcon)
            self.closeButton.setAutoresizingMask_(NSViewMaxXMargin|NSViewMaxYMargin)
            self.closeButton.setHidden_(True)
            self.addSubview_(self.closeButton)
            
            self.switcher = switcher
            self.item = item
        return self


    def updateTrackingAreas(self):
        if self.trackingArea:
            self.removeTrackingArea_(self.trackingArea)
        rect = NSZeroRect
        rect.size = self.frame().size
        tarea = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(rect,
            NSTrackingActiveInActiveApp|NSTrackingMouseEnteredAndExited, self, None)
        self.addTrackingArea_(tarea)
        self.trackingArea = tarea


    def setLabel_(self, label):
        if type(label) == NSString:
            self.label = label
        else:
            self.label = NSString.stringWithString_(label)
        frame = self.frame()
        frame.size.width = self.idealWidth()
        self.setFrame_(frame)

    def idealWidth(self):
        attribs = NSDictionary.dictionaryWithObject_forKey_(NSFont.systemFontOfSize_(11), NSFontAttributeName)
        size = self.label.sizeWithAttributes_(attribs)
        
        return size.width + 14 + 20

    def dragImage(self):
        if self.cachedDragImage:
           return self.cachedDragImage
 
        self.lockFocus()
        rep = NSBitmapImageRep.alloc().initWithFocusedViewRect_(self.bounds())
        self.unlockFocus()
        tabImage = NSImage.alloc().initWithSize_(rep.size())
        tabImage.addRepresentation_(rep)
        
        image = NSImage.alloc().initWithSize_(rep.size())
        image.addRepresentation_(rep)
        image.lockFocus()
        tabImage.compositeToPoint_operation_fraction_(NSZeroPoint, NSCompositeSourceOver, 1.0)
        image.unlockFocus()

        return image

    def drawRect_(self, rect):
        r = self.bounds()
        r.size.width -= 0.5
        r.size.height += 4
        if self.draggedOut:
            NSColor.colorWithDeviceWhite_alpha_(0.4, 1.0).set()
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 5, 5)
            path.fill()
        else:
            if self == self.switcher.activeItem():
                NSColor.controlColor().set()
            else:
                NSColor.colorWithDeviceWhite_alpha_(0.6, 1.0).set()
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 5, 5)
            path.fill()
            NSColor.colorWithDeviceRed_green_blue_alpha_(0.3, 0.3, 0.3, 1.0).set()
            path.stroke()

        if self.badgeLabel and not self.mouseInside and not self.busyIndicator and not self.composing:
            # draw the number in redbadge indicator
            gradient = NSGradient.alloc().initWithStartingColor_endingColor_(
                          NSColor.colorWithDeviceRed_green_blue_alpha_(0.9, 0.2, 0.2, 1),
                          NSColor.colorWithDeviceRed_green_blue_alpha_(1.0, 0.2, 0.2, 1)) 
            size = self.badgeLabel.size()
            size.width += 4
            if size.width < 12:
                size.width = 12
            bez = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(4, 5, size.width, 12), 6, 6)
            gradient.drawInBezierPath_angle_(bez, 90+45)
            self.badgeLabel.drawInRect_(NSMakeRect(4, 5, size.width, 12))

        if not self.mouseInside and not self.busyIndicator and self.composing:
            rect = NSZeroRect.copy()
            rect.size = self.composeIcon.size()
            self.composeIcon.drawAtPoint_fromRect_operation_fraction_(NSMakePoint(3, 3), rect, NSCompositeSourceOver, 1)

        if not self.mouseInside and not self.busyIndicator and self.screen_sharing and not self.composing:
            rect = NSZeroRect.copy()
            rect.size = self.screenIcon.size()
            self.screenIcon.drawAtPoint_fromRect_operation_fraction_(NSMakePoint(10, 3), rect, NSCompositeSourceOver, 1)

        if not self.draggedOut:
            shadow = NSShadow.alloc().init()
            shadow.setShadowOffset_(NSMakeSize(0, -1))
            if self == self.switcher.activeItem():
                shadow.setShadowColor_(NSColor.whiteColor())
            else:
                shadow.setShadowColor_(NSColor.colorWithDeviceWhite_alpha_(0.7, 1.0))
            para = NSParagraphStyle.defaultParagraphStyle().mutableCopy()
            para.setLineBreakMode_(NSLineBreakByTruncatingTail)
            para.setAlignment_(NSCenterTextAlignment)
            attribs = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(11), NSFontAttributeName,
                            shadow, NSShadowAttributeName,
                            para, NSParagraphStyleAttributeName)

            rect = self.bounds()
            rect.origin.y -= 3
            rect.origin.x += 5 + 14 + 4
            rect.size.width -= 5 + 14 + 4 + 20
            self.label.drawInRect_withAttributes_(rect, attribs)

    def mouseEntered_(self, event):
        self.mouseInside = True
        self.closeButton.setHidden_(False)
        if self.busyIndicator:
            self.busyIndicator.setHidden_(True)

        self.setNeedsDisplay_(True)
    
    
    def mouseExited_(self, event):
        self.mouseInside = False
        self.closeButton.setHidden_(True)
        if self.busyIndicator:
            self.busyIndicator.setHidden_(False)
        self.setNeedsDisplay_(True)
    
    def mouseDown_(self, event):
        self.switcher.tabView.selectTabViewItem_(self.item)
        self.switcher.startedDragging_event_(self, event)
    
    def mouseDragged_(self, event):
        self.switcher.draggedItem_event_(self, event)
        if not self.cachedDragImage:
            self.cachedDragImage = self.dragImage()
    
    def mouseUp_(self, event):
        self.switcher.finishedDraging_event_(self, event)
        self.cachedDragImage = None

    def setBadgeLabel_(self, text):
        if text:
            self.badgeLabel = NSAttributedString.alloc().initWithString_attributes_(text, self.badgeAttributes)
        else:
            self.badgeLabel = None
        self.setNeedsDisplay_(True)

    def setDraggedOut_(self, flag):
        self.draggedOut = flag
        self.setNeedsDisplay_(True)
        if self.busyIndicator:
            self.busyIndicator.setHidden_(flag)

    def setComposing_(self, flag):
        self.composing = flag
        self.setNeedsDisplay_(True)

    def setScreenSharing_(self, flag):
        self.screen_sharing = flag
        self.setNeedsDisplay_(True)

    def setBusy_(self, flag):
        if flag:
            if not self.busyIndicator:
                self.busyIndicator = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(3, 4, 15, 15))
                self.busyIndicator.setControlSize_(NSSmallControlSize)
                self.busyIndicator.setIndeterminate_(True)
                self.busyIndicator.setStyle_(NSProgressIndicatorSpinningStyle)
                self.busyIndicator.startAnimation_(None)
                self.busyIndicator.setAutoresizingMask_(NSViewMaxXMargin|NSViewMaxYMargin)
                self.addSubview_(self.busyIndicator)
                self.closeButton.setHidden_(True)
        else:
            if self.busyIndicator:
                self.busyIndicator.stopAnimation_(None)
                self.busyIndicator.removeFromSuperview()
                self.busyIndicator = None
                self.closeButton.setHidden_(not self.mouseInside)


class FancyTabSwitcher(NSView):
    delegate = objc.IBOutlet()
    tabView = objc.IBOutlet()
    dragWindow = None
    draggedOut = False
    items = None
    menu = None
    firstVisible = 0
    leftButton = None
    rightButton = None
    fitTabCount = 1

    def initWithFrame_(self, frame):
        self = NSView.initWithFrame_(self, frame)
        if self:
            self.items = []
            
            self.leftButton = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 20, 20))
            self.leftButton.setTarget_(self)
            self.leftButton.setAction_("scrollLeft:")
            self.leftButton.setImage_(NSImage.imageNamed_("NSLeftFacingTriangleTemplate"))
            self.leftButton.setHidden_(True)
            self.leftButton.cell().setBezelStyle_(NSShadowlessSquareBezelStyle)
            self.addSubview_(self.leftButton)

            self.rightButton = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 20, 20))
            self.rightButton.setTarget_(self)
            self.rightButton.setAction_("scrollRight:")
            self.rightButton.setImage_(NSImage.imageNamed_("NSRightFacingTriangleTemplate"))
            self.rightButton.setHidden_(True)
            self.rightButton.cell().setBezelStyle_(NSShadowlessSquareBezelStyle)
            self.addSubview_(self.rightButton)

        return self


    def setTabView_(self, tabView):
        self.tabView = tabView
        


    def tabView_didSelectTabViewItem_(self, tabView, item):
        index = 0
        j = 0
        for i in self.tabView.tabViewItems():
            i.view().setHidden_(i != item)
            if item == i:
                index = j
            j += 1

        self.delegate.tabView_didSelectTabViewItem_(tabView, item)
        for item in self.items:
            item.setNeedsDisplay_(True)
        if index < self.firstVisible:
            self.firstVisible = index
            self.rearrange()
        elif index >= self.firstVisible + self.fitTabCount:
            self.firstVisible = max(index - self.fitTabCount + 1, 0)
            self.rearrange()

    def tabViewDidChangeNumberOfTabViewItems_(self, tabView):
        self.delegate.tabViewDidChangeNumberOfTabViewItems_(tabView)
    
    
    def setTabViewItem_busy_(self, item, busy):
        for i in self.items:
            if i.item == item:
                i.setBusy_(busy)
                break
    
    
    def addTabViewItem_(self, item):
        label = item.label()
        titem = FancyTabItem.alloc().initWithSwitcher_item_(self, item)
        titem.setLabel_(label)
        self.addSubview_(titem)
        self.items.append(titem)
        
        self.tabView.addTabViewItem_(item)
        self.rearrange()
        
        titem.closeButton.setTarget_(self)
        titem.closeButton.setAction_("closeItemClicked:")
    
    
    def removeTabViewItem_(self, item):
        self.tabView.removeTabViewItem_(item)
        
        titem = self.itemForTabViewItem_(item)
        if titem:
            self.items.remove(titem)
            titem.removeFromSuperview()
        self.rearrange()
    
    
    def selectLastTabViewItem_(self, sender):
        self.tabView.selectLastTabViewItem_(sender)
    
    
    def closeItemClicked_(self, sender):
        item = sender.superview()
        resp = self.delegate.tabView_shouldCloseTabViewItem_(self.tabView, item.item)
        if resp:
            self.removeTabViewItem_(item.item)


    def reorderByPosition_(self, sender):
        def centerx(rect):
            return rect.origin.x + rect.size.width/2
            
        self.items.sort(lambda a,b: int(centerx(a.frame()) - centerx(b.frame())))

        frame = self.frame()
        x = 5
        h = NSHeight(frame)
        for item in self.items:
            w = item.idealWidth()
            if item != sender:
                item.setFrame_(NSMakeRect(x, 2, w, h))
            x += w


    def startedDragging_event_(self, sender, event):
        self.dragPos = sender.convertPoint_fromView_(event.locationInWindow(), None)
        #sender.setHidden_(True)
        self.draggedOut = False


    def createDragWindowForTab_(self, tabItem):
        rect = tabItem.frame()
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(rect, NSBorderlessWindowMask,
                  NSBackingStoreBuffered, False)
        window.setAlphaValue_(0.8)
        imageView = NSImageView.alloc().initWithFrame_(rect)
        imageView.setImage_(tabItem.dragImage())
        window.setContentView_(imageView)
        window.setReleasedWhenClosed_(False)
        return window


    def draggedItem_event_(self, sender, event):
        p = sender.convertPoint_fromView_(event.locationInWindow(), None)
        dx = self.dragPos.x - p.x
        dy = self.dragPos.y - p.y
        
        frame = sender.frame()
        
        if abs(dy) > 25 or self.dragWindow:
            if not self.dragWindow:
                self.dragWindow = self.createDragWindowForTab_(sender)
                self.dragWindow.makeKeyAndOrderFront_(None)
                self.draggedOut = True
                sender.setDraggedOut_(True)
            pos = NSEvent.mouseLocation()
            pos.x -= self.dragPos.x
            pos.y -= self.dragPos.y
            self.dragWindow.setFrameOrigin_(pos)

            

            if abs(dy) < 25 and p.x > NSMinX(frame) and p.x < NSMaxX(frame):
                self.dragWindow.close()
                self.dragWindow = None
                self.draggedOut = False
                sender.setDraggedOut_(False)
            else:
                return

        frame.origin.x -= dx
        sender.setFrame_(frame)

        self.reorderByPosition_(sender)

    
    def finishedDraging_event_(self, sender, event):
        sender.setHidden_(False)
        self.rearrange()
        if self.dragWindow:
            self.dragWindow.close()
            self.dragWindow = None
            sender.setDraggedOut_(False)
        if self.draggedOut:
            self.delegate.tabView_didDettachTabViewItem_atPosition_(self.tabView, sender.item, NSEvent.mouseLocation())
            self.draggedOut = False


    def rearrange(self):
        if not self.items:
            return

        frame = self.frame()
        x = 5
        h = NSHeight(frame)
                
        if len(self.items) * MIN_TAB_WIDTH > NSWidth(frame) - 15 - 20:
            # some tabs don't fit, show what we can
            self.fitTabCount = max(int(NSWidth(frame)-15-40) / MIN_TAB_WIDTH, 1)
            tab_width = int(NSWidth(frame)-15-40) / self.fitTabCount

            self.leftButton.setFrame_(NSMakeRect(0, 3, 24, 20))
            self.rightButton.setFrame_(NSMakeRect(NSWidth(frame)-31, 3, 24, 20))
            self.leftButton.setHidden_(False)
            self.rightButton.setHidden_(False)
            
            x += 20
            for item in self.items[self.firstVisible : self.firstVisible + self.fitTabCount]:
                item.setHidden_(False)
                item.setFrame_(NSMakeRect(x, 2, tab_width, h))
                x += tab_width

            for item in self.items[self.firstVisible + self.fitTabCount :]:
                item.setHidden_(True)

            for item in self.items[: self.firstVisible]:
                item.setHidden_(True)

        else:
            self.leftButton.setHidden_(True)
            self.rightButton.setHidden_(True)

            tab_width = min(TAB_WIDTH, (NSWidth(frame) - 15) / len(self.items))
            for item in self.items:
                w = tab_width
                item.setFrame_(NSMakeRect(x, 2, w, h))
                item.setHidden_(False)
                x += w
    
    
    def resizeSubviewsWithOldSize_(self, osize):
        self.rearrange()
    
    
    def drawRect_(self, rect):
        gradient = NSGradient.alloc().initWithColors_(
                    [NSColor.colorWithDeviceRed_green_blue_alpha_(121/256.0, 121/256.0, 121/256.0, 1),
                     NSColor.colorWithDeviceRed_green_blue_alpha_(111/256.0, 111/256.0, 111/256.0, 1)])
        gradient.drawInRect_angle_(rect, 90.0)
        NSView.drawRect_(self, rect)


    def itemForTabViewItem_(self, item):
        for i in self.items:
            if i.item == item:
                return i
        return None

    def scrollLeft_(self, sender):
        if self.firstVisible > 0:
            self.firstVisible -= 1
        self.rearrange()

    def scrollRight_(self, sender):
        if self.firstVisible < len(self.items) - self.fitTabCount:
            self.firstVisible += 1
        self.rearrange()

    def activeItem(self):
        item = self.tabView.selectedTabViewItem()
        if not item:
            return None
        return self.itemForTabViewItem_(item)
    


