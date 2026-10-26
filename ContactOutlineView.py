# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSArray,
                    NSBeep,
                    NSIndexSet,
                    NSMenu,
                    NSMenuItem,
                    NSNotFound,
                    NSOffState,
                    NSOnState,
                    NSPointInRect,
                    NSOutlineView,
                    NSPasteboard,
                    NSStringPboardType)

from ContactListModel import BlinkContact, BlinkGroup, GROUP_SORT_NAME, GROUP_SORT_RECENT, GROUP_SORT_UNREAD
from ContactCell import group_sort_label, group_sort_label_rect
from Foundation import NSInsetRect, NSLocalizedString

import objc


class ContactOutlineView(NSOutlineView):
    def mouseDown_(self, event):
        # A click on a group header's sort control picks how the group is
        # sorted; anywhere else on the header it expands and collapses as
        # it always has.
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        row = self.rowAtPoint_(point)
        item = self.itemAtRow_(row) if row >= 0 else None
        if isinstance(item, BlinkGroup):
            label = group_sort_label(item)
            if label is not None:
                rect = group_sort_label_rect(self.frameOfCellAtColumn_row_(0, row), self, label)
                if NSPointInRect(point, NSInsetRect(rect, -4, -3)):
                    self.showSortMenuForGroup(item, rect)
                    return
        NSOutlineView.mouseDown_(self, event)

    @objc.python_method
    def showSortMenuForGroup(self, group, rect):
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        titles = {GROUP_SORT_NAME: NSLocalizedString("Alphabetically", "Group sort order menu item"),
                  GROUP_SORT_RECENT: (NSLocalizedString("By Last Call", "Group sort order menu item")
                                      if group.usesCallTimes()
                                      else NSLocalizedString("By Last Message", "Group sort order menu item")),
                  GROUP_SORT_UNREAD: NSLocalizedString("By Unread Messages", "Group sort order menu item")}
        current = group.effectiveSortMode()
        for mode in group.availableSortModes():
            item = menu.addItemWithTitle_action_keyEquivalent_(titles[mode], "sortGroupBy:", "")
            item.setTarget_(self)
            item.setRepresentedObject_((group, mode))
            item.setState_(NSOnState if mode == current else NSOffState)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, (rect.origin.x, rect.origin.y + rect.size.height + 2), self)

    def sortGroupBy_(self, sender):
        group, mode = sender.representedObject()
        if mode == group.effectiveSortMode():
            return
        # Keep the selected contact selected: the rows move under it.
        row = self.selectedRow()
        selected = self.itemAtRow_(row) if row >= 0 else None
        group.setSortMode(mode)
        self.reloadItem_reloadChildren_(group, True)
        if selected is not None:
            row = self.rowForItem_(selected)
            if row >= 0:
                self.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)

    def menuForEvent_(self, event):
        self.window().makeFirstResponder_(self)
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        row = self.rowAtPoint_(point)
        if row < 0:
            return None
        # Right-clicking inside a multiple selection acts on the selection;
        # anywhere else it selects the clicked row, as before.
        if not (self.numberOfSelectedRows() > 1 and self.isRowSelected_(row)):
            self.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
        return self.menu()

    def keyDown_(self, event):
        key = event.characters()
        if key == "\r":
            self.target().performSelector_withObject_(self.doubleAction(), self)
            return

        # Delete (backspace), and forward delete, on a selected row is a
        # request to delete that row. It must reach the same confirmation the
        # context menu's Delete opens. Below, anything that is not navigation
        # is handed to the search box as the start of a query, which is how
        # Delete used to land the focus in the search bar and do nothing else.
        # With no row selected there is nothing to delete, so the key keeps
        # its old meaning and edits the query.
        if key and key[0] in ("\x7f", "\x08", "\uf728"):
            row = self.selectedRow()
            if self.numberOfSelectedRows() > 1:
                # deleteItem_ sees the multiple selection and asks once for all.
                self.target().deleteItem_(self)
                return
            if row >= 0:
                item = self.itemAtRow_(row)
                if item is None or not getattr(item, "deletable", False):
                    NSBeep()
                else:
                    self.target().deleteItem_(self)
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

