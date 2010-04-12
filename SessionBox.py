# Copyright (C) 2009 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *

from VerticalBoxView import VerticalBoxView


class SessionBox(NSView):
    selected = False
    delegate = None
    sessionInfo = None
    highlighted = False
    conferencing = False
    draggedOut = False
    dragPos = NSZeroPoint

    def acceptsFirstResponder(self):
        return True

    def canBecomeKeyView(self):
        return True

    def setSessionInfo_(self, info):
        self.sessionInfo = info

    def copy_(self, sender):
        pb = NSPasteboard.generalPasteboard()
        if self.sessionInfo:
            pb.declareTypes_owner_(NSArray.arrayWithObject_(NSStringPboardType), self)
            pb.setString_forType_(self.sessionInfo, NSStringPboardType)

    def awakeFromNib(self):
        self.registerForDraggedTypes_(NSArray.arrayWithObjects_("x-blink-audio-session", "x-blink-sip-uri"))

    def keyDown_(self, event):
        if self.delegate:
            try:
                chars = str(event.characters())
                if chars:
                    self.delegate.sessionBoxKeyPressed(self, chars[0])
            except:
                pass
        if self.window():
            self.window().makeFirstResponder_(self)

    def setDelegate_(self, delegate):
        self.delegate = delegate

    def makeDragImage(self):
        image = NSImage.alloc().initWithSize_(self.frame().size)
        image.lockFocus()
        
        frame = self.frame()
        frame.origin = NSZeroPoint
        rect = NSInsetRect(frame, 1.5, 1.5)

        if self.conferencing and not self.draggedOut:
            NSColor.selectedControlColor().colorWithAlphaComponent_(0.7).set()
        else:
            NSColor.whiteColor().colorWithAlphaComponent_(0.7).set()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 5.0, 5.0)
        path.fill()

        if self.selected:
            path.setLineWidth_(3)
            NSColor.grayColor().set()
        else:
            path.setLineWidth_(1)
            NSColor.grayColor().set()
        path.stroke()

        NSColor.blackColor().set()
        point = NSMakePoint(8, NSMaxY(frame)-20)
        NSString.stringWithString_(self.sessionInfo).drawAtPoint_withAttributes_(point, 
              NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.boldSystemFontOfSize_(12), NSFontAttributeName))
        point = NSMakePoint(8, 6)
        if self.conferencing:
            NSString.stringWithString_("Drop outside to remove from conference").drawAtPoint_withAttributes_(point, 
                  NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(10), NSFontAttributeName))
        else:
            NSString.stringWithString_("Drop over an audio session to conference them").drawAtPoint_withAttributes_(point, 
                  NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(10), NSFontAttributeName))
        
        icon = NSImage.imageNamed_("NSUserGroup")
        rect = frame
        s = icon.size()
        p = NSMakePoint(NSWidth(rect) - s.width - 8, rect.size.height - s.height - 8)
        r = NSMakeRect(0, 0, s.width, s.height)
        icon.drawAtPoint_fromRect_operation_fraction_(p, r, NSCompositeSourceOver, 0.5)
        
        image.unlockFocus()
        return image
    
    def mouseUp_(self, event):
        self.setSelected_(True)
        self.window().makeFirstResponder_(self)
    
    def mouseDown_(self, event):
        self.dragPos = event.locationInWindow()

    def mouseDragged_(self, event):
        pos = event.locationInWindow()
        if abs(self.dragPos.x - pos.x) > 3 or abs(self.dragPos.y - pos.y) > 3:
            image = self.makeDragImage()
            
            pos.x -= image.size().width/2
            pos.y -= image.size().height/2
            pboard = NSPasteboard.pasteboardWithName_(NSDragPboard)
            pboard.declareTypes_owner_(NSArray.arrayWithObject_("x-blink-audio-session"), self)
            pboard.setString_forType_(self.sessionInfo, "x-blink-audio-session")
            self.window().dragImage_at_offset_event_pasteboard_source_slideBack_(image, 
                    pos, NSZeroPoint, event, pboard, self, False)
            self.draggedOut = False
            

    def viewDidMoveToSuperview(self):
        if self.selected and self.superview():
            # unselect the other views in the superview
            for view in self.superview().subviews():
                if view != self and isinstance(view, SessionBox):
                    if view.selected and not (self.conferencing and view.conferencing):
                        view.setSelected_(False)

    def viewDidMoveToWindow(self):
        if self.selected and self.window():
            self.window().makeFirstResponder_(self)

    def setSelected_(self, flag):
        if self.selected != flag:
            self.selected = flag
            self.setNeedsDisplay_(True)
            if flag:
                if self.superview():
                    for view in self.superview().subviews():
                        if view != self and isinstance(view, SessionBox):
                            if self.conferencing and view.conferencing:
                                if not view.selected:
                                    view.selected = True
                                    view.setNeedsDisplay_(True)
                                    if view.delegate and getattr(view.delegate, "sessionBoxDidActivate"):
                                        view.delegate.sessionBoxDidActivate(self)
                            elif view.selected:
                                view.setSelected_(False)
                    self.window().makeFirstResponder_(self)
                if self.delegate and getattr(self.delegate, "sessionBoxDidActivate"):
                    self.delegate.sessionBoxDidActivate(self)
            else:
                if self.delegate and getattr(self.delegate, "sessionBoxDidDeactivate"):
                    self.delegate.sessionBoxDidDeactivate(self)

    def draggedImage_endedAt_operation_(self, image, point, operation):
        if self.draggedOut and self.conferencing:
            self.delegate.sessionBoxDidRemoveFromConference(self)

    def foreachConferenceSession(self, callable):
        for view in self.superview().subviews():
            if view.conferencing:
                callable(view)

    def draggingEntered_(self, info):
        def highlight(view):
            view.highlighted = True
            view.setNeedsDisplay_(True)

        if info.draggingPasteboard().availableTypeFromArray_(["x-blink-sip-uri"]):
            # contact
            highlight(self)
            self.foreachConferenceSession(highlight)
            return NSDragOperationAll
        else:
            if (info.draggingSource() == self and not self.conferencing) or not info.draggingSource():
                return NSDragOperationNone
            # drop over a session that's not in a conference while there is 1 ongoing 
            if not self.conferencing and self.superview().subviews().objectAtIndex_(0).conferencing:
                return NSDragOperationNone
            if not info.draggingSource().delegate.canConference and not self.delegate.canConference:
                return NSDragOperationNone

            highlight(self)
            self.foreachConferenceSession(highlight)
            
            info.draggingSource().draggedOut = False
            info.draggingSource().makeDragImage()

            return NSDragOperationAll
    
    def draggingExited_(self, info):
        def unhighlight(view):
            view.highlighted = False
            view.setNeedsDisplay_(True)
        unhighlight(self)
        self.foreachConferenceSession(unhighlight)

        if info.draggingPasteboard().availableTypeFromArray_(["x-blink-audio-session"]):
            info.draggingSource().draggedOut = True
            info.draggingSource().setNeedsDisplay_(True)

    def performDragOperation_(self, info):
        source = info.draggingSource()
        pboard = info.draggingPasteboard()
        
        def unhighlight(view):
            view.highlighted = False
            view.setNeedsDisplay_(True)
        unhighlight(self)
        self.foreachConferenceSession(unhighlight)

        if pboard.availableTypeFromArray_(["x-blink-audio-session"]):
            info.draggingSource().draggedOut = False
            info.draggingSource().setNeedsDisplay_(True)
            return self.delegate.sessionBoxDidAddConferencePeer(self, source.delegate)
        elif pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
            uri = str(pboard.stringForType_("x-blink-sip-uri"))
            return self.delegate.sessionBoxDidAddConferencePeer(self, uri)

    def setConferencing_(self, flag):
        self.conferencing = flag
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        rect = NSInsetRect(self.bounds(), 1.5, 1.5)
        if not self.conferencing:
            NSColor.whiteColor().set()
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 5.0, 5.0)
            path.fill()
            
        if self.conferencing:
            if self.draggedOut:
                NSColor.whiteColor().set()
            else:
                # bgcolor for conference area
                NSColor.colorWithDeviceRed_green_blue_alpha_(196/255.0, 230/255.0, 254/255.0, 1.0).set()
            border = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 5.0, 5.0)
            border.setLineWidth_(1)
            border.fill()
            NSColor.grayColor().set()
            border.stroke()

            # hack: if we're the 1st item, draw the border around all conferenced boxes
            subviews = self.superview().subviews()
            if subviews.objectAtIndex_(0) == self:
                # first in conference list
                rect.size.height += 5
                rect.origin.y -= 5
                path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 5.0, 5.0)
            else:
                prev = None
                last = True
                for view in subviews:
                    if prev == self:
                        last = not view.conferencing
                        break
                    prev = view
                # last in conference list
                if last:
                    rect.size.height += 5
                    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 5.0, 5.0)
                else:
                    rect.origin.y -= 5
                    rect.size.height += 10
                    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 5.0, 5.0)

            if self.selected or self.highlighted:
                path.setLineWidth_(3)
            else:
                path.setLineWidth_(1)
            if self.highlighted:
                NSColor.orangeColor().set()
            else:
                NSColor.grayColor().set()
            if self.selected or self.highlighted:
                path.stroke()
        elif self.highlighted:
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 5.0, 5.0)
            path.setLineWidth_(3)
            NSColor.orangeColor().set()
            path.stroke()
        elif self.selected:
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 5.0, 5.0)
            path.setLineWidth_(3)
            NSColor.grayColor().set()
            path.stroke()            
        else:
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 5.0, 5.0)
            path.setLineWidth_(1)
            NSColor.grayColor().set()
            path.stroke()


