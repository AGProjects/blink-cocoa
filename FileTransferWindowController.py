# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

from AppKit import *
from Foundation import *

import unicodedata

from application.notification import NotificationCenter, IObserver
from application.python.util import Null
from sipsimple.threading.green import run_in_green_thread
from zope.interface import implements

import ListView
from HistoryManager import FileTransferHistory
from FileTransferItem import FileTransferItem
from util import allocate_autorelease_pool, run_in_gui_thread

import SIPManager


def openFileTransferSelectionDialog(account, dest_uri):
    if not SIPManager.SIPManager().isMediaTypeSupported('file-transfer'):
        return

    panel = NSOpenPanel.openPanel()
    panel.setTitle_(u"Send File")
    panel.setAllowsMultipleSelection_(True)
    if panel.runModal() != NSOKButton:
        return
    filenames = [unicodedata.normalize('NFC', file) for file in panel.filenames()]
    SIPManager.SIPManager().send_files_to_contact(account, dest_uri, filenames)


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

    @run_in_green_thread
    @allocate_autorelease_pool
    def get_previous_transfers(self, active_items=[]):
        try:
            results = FileTransferHistory().get_transfers()
            transfers = [transfer for transfer in reversed(list(results)) if transfer.transfer_id not in active_items]
            self.render_previous_transfers(transfers)
        except:
            pass

    @run_in_gui_thread
    def render_previous_transfers(self, transfers):
        last_displayed_item = self.listView.subviews().lastObject()

        for transfer in transfers:
            item = FileTransferItem.alloc().initWithFrame_oldTransfer_(NSMakeRect(0, 0, 100, 100), transfer)

            if last_displayed_item:
                self.listView.insertItemView_before_(item, last_displayed_item)
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

    def refresh(self):
        active_items = []
        for item in self.listView.subviews().copy():
            if item.done:
                item.removeFromSuperview()
            else:
                if item.transfer:
                    active_items.append(item.transfer.transfer_id)

        self.listView.relayout()
        self.listView.display()

        self.get_previous_transfers(active_items)

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
        if SIPManager.SIPManager().isMediaTypeSupported('file-transfer'):
            self.window.makeKeyAndOrderFront_(None)

    @run_in_green_thread
    def delete_history_transfers(self):
        FileTransferHistory().delete_transfers()

    @objc.IBAction
    def clearList_(self, sender):
        self.delete_history_transfers()
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


