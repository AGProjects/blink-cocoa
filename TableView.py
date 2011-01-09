# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import *


class TableView(NSTableView):
    def keyDown_(self, event):
        if not event.isARepeat() and event.charactersIgnoringModifiers().characterAtIndex_(0) == 127: # delete
            row = self.selectedRow()
            if row >= 0:
                self.dataSource().tableView_setObjectValue_forTableColumn_row_(self, None, None, row)
        else:
            NSTableView.keyDown_(self, event)