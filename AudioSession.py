# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSCompositeSourceOver,
                    NSDragOperationAll,
                    NSDragOperationCopy,
                    NSDragOperationNone,
                    NSDragPboard,
                    NSFilenamesPboardType,
                    NSFontAttributeName,
                    NSLeftMouseUp,
                    NSStringPboardType)

from Foundation import (NSArray,
                        NSBezierPath,
                        NSColor,
                        NSDate,
                        NSDictionary,
                        NSEvent,
                        NSFont,
                        NSImage,
                        NSInsetRect,
                        NSMakePoint,
                        NSMakeRect,
                        NSMaxY,
                        NSMenu,
                        NSPasteboard,
                        NSString,
                        NSLocalizedString,
                        NSView,
                        NSWidth,
                        NSZeroPoint)

import objc
import os
import re
import time
import unicodedata

from sipsimple.threading import call_in_thread

from VerticalBoxView import VerticalBoxView
from util import format_identity_to_string


class AudioSession(NSView):
    selected = False
    delegate = None
    highlighted = False
    conferencing = False
    draggedOut = False
    dragPos = NSZeroPoint

    @property
    def sessionControllersManager(self):
        return NSApp.delegate().contactsWindowController.sessionControllersManager

    def dealloc(self):
        objc.super(AudioSession, self).dealloc()

    def acceptsFirstResponder(self):
        return True

    @objc.python_method
    def canBecomeKeyView(self):
        return True

    def copy_(self, sender):
        if self.delegate is None:
            return
        pb = NSPasteboard.generalPasteboard()
        copy_text = format_identity_to_string(self.delegate.sessionController.remoteIdentity, check_contact=True, format='full')
        pb.declareTypes_owner_(NSArray.arrayWithObject_(NSStringPboardType), self)
        pb.setString_forType_(copy_text, NSStringPboardType)

    def paste_(self, sender):
        pasteboard = NSPasteboard.generalPasteboard()
        digits = pasteboard.stringForType_("public.utf8-plain-text")
        dtmf_match_regexp = re.compile("^([0-9]#*)+$")
        if digits and dtmf_match_regexp.match(digits):
            call_in_thread('dtmf-io', self.send_dtmf, digits)

    @objc.python_method
    def send_dtmf(self, digits):
        for digit in digits:
            time.sleep(0.5)
            if digit == ',':
                time.sleep(1)
            else:
                if self.delegate is not None:
                    self.delegate.send_dtmf(digit)

    def awakeFromNib(self):
        self.registerForDraggedTypes_(NSArray.arrayWithObjects_("x-blink-audio-session", "x-blink-sip-uri", NSFilenamesPboardType))

    def keyDown_(self, event):
        if self.delegate:
            try:
                self.delegate.sessionBoxKeyPressEvent(self, event)
            except:
                pass
        if self.window():
            self.window().makeFirstResponder_(self)

    def setDelegate_(self, delegate):
        self.delegate = delegate

    @objc.python_method
    def makeDragImage(self):
        if self.delegate is None:
            return

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
        uri = format_identity_to_string(self.delegate.sessionController.remoteIdentity, check_contact=False, format='compact')
        NSString.stringWithString_(uri).drawAtPoint_withAttributes_(point,
              NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.boldSystemFontOfSize_(12), NSFontAttributeName))
        point = NSMakePoint(8, 6)
        if self.conferencing:
            NSString.stringWithString_(NSLocalizedString("Drop outside to remove from conference", "Audio status label")).drawAtPoint_withAttributes_(point,
                  NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(10), NSFontAttributeName))
        else:
            audio_sessions = [sess.hasStreamOfType("audio") for sess in NSApp.delegate().contactsWindowController.sessionControllersManager.sessionControllers]
            if self.delegate.transferEnabled:
                text = NSLocalizedString("Drop this over a session or contact", "Audio status label") if len(audio_sessions) > 1 else NSLocalizedString("Drop this over a contact to transfer", "Audio status label")
            else:
                text = NSLocalizedString("Drop this over a session to conference", "Audio status label")
            NSString.stringWithString_(text).drawAtPoint_withAttributes_(point,
                  NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(10), NSFontAttributeName))

        icon = NSImage.imageNamed_("NSEveryone")
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
        if self.delegate is None:
            return
        pos = event.locationInWindow()
        if abs(self.dragPos.x - pos.x) > 3 or abs(self.dragPos.y - pos.y) > 3:
            image = self.makeDragImage()

            pos.x -= image.size().width/2
            pos.y -= image.size().height/2
            pboard = NSPasteboard.pasteboardWithName_(NSDragPboard)
            pboard.declareTypes_owner_(NSArray.arrayWithObject_("x-blink-audio-session"), self)
            uri = format_identity_to_string(self.delegate.sessionController.remoteIdentity, check_contact=False, format='compact')
            pboard.setString_forType_(uri, "x-blink-audio-session")
            self.window().dragImage_at_offset_event_pasteboard_source_slideBack_(image,
                    pos, NSZeroPoint, event, pboard, self, False)
            self.draggedOut = False


    def viewDidMoveToSuperview(self):
        if self.selected and self.superview():
            # unselect the other views in the superview
            for view in self.superview().subviews():
                if view != self and isinstance(view, AudioSession):
                    if view.selected and not (self.conferencing and view.conferencing):
                        view.setSelected_(False)

    @objc.python_method
    def viewDidMoveToWindow(self):
        if self.selected and self.window():
            self.window().makeFirstResponder_(self)

    def setSelected_(self, flag):
        self.selected = flag
        self.setNeedsDisplay_(True)
        if flag:
            if self.superview():
                for view in self.superview().subviews():
                    if view != self and isinstance(view, AudioSession):
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
        if self.delegate is None:
            return
        if self.draggedOut and self.conferencing:
            self.delegate.sessionBoxDidRemoveFromConference(self)

    @objc.python_method
    def foreachConferenceSession(self, callable):
        for view in self.superview().subviews():
            if view.conferencing:
                callable(view)

    def draggingEntered_(self, info):
        if self.delegate is None:
            return NSDragOperationNone

        @objc.python_method
        def highlight(view):
            view.highlighted = True
            view.setNeedsDisplay_(True)

        if info.draggingPasteboard().availableTypeFromArray_([NSFilenamesPboardType]):
            fnames = info.draggingPasteboard().propertyListForType_(NSFilenamesPboardType)
            for f in fnames:
                if not os.path.isfile(f) and not os.path.isdir(f):
                    return NSDragOperationNone
            return NSDragOperationCopy
        elif info.draggingPasteboard().availableTypeFromArray_(["x-blink-sip-uri"]):
            # contact
            highlight(self)
            self.foreachConferenceSession(highlight)
            return NSDragOperationAll
        else:
            source = info.draggingSource()
            if (source == self and not self.conferencing) or not source:
                return NSDragOperationNone
            # drop over a session that's not in a conference while there is 1 ongoing
            if not self.conferencing and self.superview().subviews().objectAtIndex_(0).conferencing:
                return NSDragOperationNone
            if source.delegate is None:
                return NSDragOperationNone
            if not source.delegate.canConference and not self.delegate.canConference:
                return NSDragOperationNone

            highlight(self)
            self.foreachConferenceSession(highlight)

            source.draggedOut = False
            source.makeDragImage()

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
        if self.delegate is None:
            return

        source = info.draggingSource()
        pboard = info.draggingPasteboard()

        if pboard.types().containsObject_(NSFilenamesPboardType):
            filenames = [unicodedata.normalize('NFC', file) for file in pboard.propertyListForType_(NSFilenamesPboardType) if os.path.isfile(file) or os.path.isdir(file)]
            if filenames:
                self.sessionControllersManager.send_files_to_contact(self.delegate.sessionController.account, self.delegate.sessionController.target_uri, filenames)
            return

        def unhighlight(view):
            view.highlighted = False
            view.setNeedsDisplay_(True)
        unhighlight(self)
        self.foreachConferenceSession(unhighlight)

        if hasattr(self.delegate, 'sessionBoxDidAddConferencePeer'):
            if pboard.availableTypeFromArray_(["x-blink-audio-session"]):
                info.draggingSource().draggedOut = False
                info.draggingSource().setNeedsDisplay_(True)
                return self.delegate.sessionBoxDidAddConferencePeer(self, source.delegate)

            elif pboard.availableTypeFromArray_(["dragged-contact"]):
                group, blink_contact = eval(pboard.stringForType_("dragged-contact"))
                if blink_contact is not None:
                    sourceGroup = NSApp.delegate().contactsWindowController.model.groupsList[group]
                    sourceContact = sourceGroup.contacts[blink_contact]

                    if len(sourceContact.uris) > 1:
                        point = self.window().convertScreenToBase_(NSEvent.mouseLocation())
                        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                                                                                                                                  NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), self.window().windowNumber(),
                                                                                                                                                  self.window().graphicsContext(), 0, 1, 0)
                        invite_menu = NSMenu.alloc().init()
                        titem = invite_menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Invite To Conference", "Menu item"), "", "")
                        titem.setEnabled_(False)
                        for uri in sourceContact.uris:
                            titem = invite_menu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (uri.uri, uri.type), "userClickedInviteToConference:", "")
                            titem.setIndentationLevel_(1)
                            titem.setTarget_(self)
                            titem.setRepresentedObject_(str(uri.uri))

                        NSMenu.popUpContextMenu_withEvent_forView_(invite_menu, event, self)
                    elif pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
                        uri = str(pboard.stringForType_("x-blink-sip-uri"))
                        return self.delegate.sessionBoxDidAddConferencePeer(self, uri)

            elif pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
                uri = str(pboard.stringForType_("x-blink-sip-uri"))
                return self.delegate.sessionBoxDidAddConferencePeer(self, uri)

    @objc.IBAction
    def userClickedInviteToConference_(self, sender):
        uri = sender.representedObject()
        self.delegate.sessionBoxDidAddConferencePeer(self, uri)

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


