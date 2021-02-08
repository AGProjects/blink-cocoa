# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSArray,
                    NSIndexSet,
                    NSNotFound,
                    NSOutlineView,
                    NSPasteboard,
                    NSStringPboardType)

from ContactListModel import BlinkContact, BlinkGroup


class ContactOutlineView(NSOutlineView):
    def menuForEvent_(self, event):
        self.window().makeFirstResponder_(self)
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        row = self.rowAtPoint_(point)
        if row < 0:
            return None
        self.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
        return self.menu()

    def keyDown_(self, event):
        key = event.characters()
        if key == "\r":
            self.target().performSelector_withObject_(self.doubleAction(), self)
        else:
            self.window().makeFirstResponder_(NSApp.delegate().contactsWindowController.searchBox)

    def acceptsFirstResponder(self):
        return True

    def copy_(self, sender):
        text = None
        selection = self.selectedRowIndexes()
        item = selection.firstIndex()
        if item != NSNotFound:
            object = self.itemAtRow_(item)
            if isinstance(object, BlinkContact):
                text = '%s <%s>' % (object.name, object.uri) if object.name != object.uri else object.uri
            elif isinstance(object, BlinkGroup):
                text = '%s' % object.name
        if text:
            pb = NSPasteboard.generalPasteboard()
            pb.declareTypes_owner_(NSArray.arrayWithObject_(NSStringPboardType), self)
            pb.setString_forType_(text, NSStringPboardType)

