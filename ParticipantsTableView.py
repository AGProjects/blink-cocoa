# Copyright (C) 2010-2026 AG Projects. See LICENSE for details.
#

from AppKit import NSIndexSet, NSTableView

from application.notification import NotificationCenter


class ParticipantsTableView(NSTableView):

    def menuForEvent_(self, event):
        self.window().makeFirstResponder_(self)

        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        row = self.rowAtPoint_(point)

        if row != -1:
            self.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
            # a WebRTC room's participant (the audio drawer, its room tile selected): the room window's own menu
            # for that participant (mute, lower hand, speaker, remove: the room's primitives), not the SIP one
            room_menu = getattr(self.delegate(), 'roomParticipantMenu', None)
            menu = room_menu(row) if room_menu is not None else None
            if menu is not None:
                return menu
            return self.menu()

    def mouseDown_(self, event):
        NotificationCenter().post_notification("BlinkTableViewSelectionChaged", sender=self)
        NSTableView.mouseDown_(self, event)

