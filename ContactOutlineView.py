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
            return

        # Arrows, page up/down, home/end and the function keys all live in
        # the Unicode private-use block AppKit reserves for them. They are
        # navigation, not typing: they must move the selection through the
        # contact list. Only real characters mean "the user started typing a
        # search", and those are what get handed to the search box.
        if key and 0xF700 <= ord(key[0]) <= 0xF8FF:
            NSOutlineView.keyDown_(self, event)
            return

        searchBox = NSApp.delegate().contactsWindowController.searchBox
        self.window().makeFirstResponder_(searchBox)
        # Without this the keystroke that moved focus is swallowed and the
        # user has to type the first letter of their search twice.
        editor = searchBox.currentEditor()
        if editor is not None:
            editor.keyDown_(event)

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

