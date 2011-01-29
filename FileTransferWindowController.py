# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

from AppKit import *
from Foundation import *

from application.notification import NotificationCenter, IObserver
from application.python.util import Null
from zope.interface import implements

import ListView
from SessionHistory import SessionHistory
from FileTransferItem import FileTransferItem
from util import allocate_autorelease_pool, run_in_gui_thread

import SIPManager

def openFileTransferSelectionDialog(account, dest_uri):
    panel = NSOpenPanel.openPanel()
    panel.setTitle_(u"Send File")
    panel.setAllowsMultipleSelection_(True)
    if panel.runModal() != NSOKButton:
        return

    try:
        names_and_types = []
        for file in panel.filenames():
            ctype, error = NSWorkspace.sharedWorkspace().typeOfFile_error_(file, None)
            if ctype:
                names_and_types.append((str(file), str(ctype)))
            else:
                print "%f : %s"%(file,error)
        if names_and_types:
            SIPManager.SIPManager().send_files_to_contact(account, dest_uri, names_and_types)
    except:
        import traceback
        traceback.print_exc()


class FileTransferWindowController(NSObject, object):
    implements(IObserver)

    window = objc.IBOutlet()
    listView = objc.IBOutlet()
    bottomLabel = objc.IBOutlet()
    history = []

    def init(self):
        NotificationCenter().add_observer(self, name="BlinkFileTransferInitializing")
        NotificationCenter().add_observer(self, name="BlinkFileTransferRestarting")
        NotificationCenter().add_observer(self, name="BlinkFileTransferDidFail")
        NotificationCenter().add_observer(self, name="BlinkFileTransferDidEnd")

        NotificationCenter().add_observer(self, name="SIPApplicationDidStart")

        NSBundle.loadNibNamed_owner_("FileTransfers", self)

        return self

    def refresh(self):
        active_items = []
        for item in self.listView.subviews().copy():
            if item.done:
                item.removeFromSuperview()
            else:
                if item.transfer:
                    active_items.append(item.transfer.transfer_log_id)

        last = self.listView.subviews().lastObject()

        entries = SessionHistory().file_transfer_log
        for entry in entries:
            if entry["id"] in active_items:
                continue
            item = FileTransferItem.alloc().initWithFrame_oldTransfer_(NSMakeRect(0, 0, 100, 100), entry)
            if last:
                self.listView.insertItemView_before_(item, last)
            else:
                self.listView.addItemView_(item)

        self.listView.relayout()
        self.listView.display()

        count = len(self.listView.subviews())
        if count == 1:
            self.bottomLabel.setStringValue_(u"1 item")
        else:
            self.bottomLabel.setStringValue_(u"%i items"%count)
        h = self.listView.minimumHeight()
        self.listView.scrollRectToVisible_(NSMakeRect(0, h-1, 100, 1))

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    @objc.IBAction
    def close_(self, sender):
        self.window.close()

    @objc.IBAction
    def showWindow_(self, sender):
        self.window.makeKeyAndOrderFront_(None)

    @objc.IBAction
    def clearList_(self, sender):
        SessionHistory().clear_transfer_history()
        self.refresh()

    def _NH_SIPApplicationDidStart(self, sender, data):
        self.refresh()

    def _NH_BlinkFileTransferRestarting(self, sender, data):
        self.listView.relayout()

    def _NH_BlinkFileTransferInitializing(self, sender, data):
        item = FileTransferItem.alloc().initWithFrame_transfer_(NSMakeRect(0, 0, 100, 100), sender)

        self.listView.addItemView_(item)
        h = NSHeight(self.listView.frame())
        self.listView.scrollRectToVisible_(NSMakeRect(0, h-1, 100, 1))

        self.window.makeKeyAndOrderFront_(None)

        count = len(self.listView.subviews())
        if count == 1:
            self.bottomLabel.setStringValue_(u"1 item")
        else:
            self.bottomLabel.setStringValue_(u"%i items"%count)

    def _NH_BlinkFileTransferDidFail(self, sender, data):
        self.listView.relayout()

    def _NH_BlinkFileTransferDidEnd(self, sender, data):
        self.listView.relayout()
        # jump dock icon and bring dl window to front
        self.window.orderFront_(None)
        NSApp.requestUserAttention_(NSInformationalRequest)


