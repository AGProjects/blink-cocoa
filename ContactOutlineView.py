# Copyright (C) 2009 AG Projects. See LICENSE for details.
#

from AppKit import *


class ContactOutlineView(NSOutlineView):
    def menuForEvent_(self, event):
        self.window().makeFirstResponder_(self)
        
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        row = self.rowAtPoint_(point)

        self.selectRow_byExtendingSelection_(row, False)

        return self.menu()
        

    def keyDown_(self, event):
        if event.characters() == "\r":
            self.target().performSelector_withObject_(self.doubleAction(), self)
        else:
            super(ContactOutlineView, self).keyDown_(event)
