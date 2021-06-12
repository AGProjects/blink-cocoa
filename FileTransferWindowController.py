# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSInformationalRequest,
                    NSOKButton)

from Foundation import (NSBundle,
                        NSHeight,
                        NSLocalizedString,
                        NSMakeRect,
                        NSObject,
                        NSOpenPanel,
                        NSURL)

import objc
import unicodedata

from application.notification import NotificationCenter, IObserver
from application.python import Null
from sipsimple.threading.green import run_in_green_thread
from zope.interface import implementer

import ListView
from BlinkLogger import BlinkLogger
from HistoryManager import FileTransferHistory
from FileTransferItemView import FileTransferItemView
from FileTransferSession import IncomingFileTransferHandler, OutgoingPushFileTransferHandler, OutgoingPullFileTransferHandler
from util import run_in_gui_thread, format_size


def openFileTransferSelectionDialog(account, dest_uri, filename=None):
    if not NSApp.delegate().contactsWindowController.sessionControllersManager.isMediaTypeSupported('file-transfer'):
        return

    panel = NSOpenPanel.openPanel()
    panel.setTitle_(NSLocalizedString("Select Files or Folders and Click Open to Send", "Window title"))
    panel.setDirectoryURL_(NSURL.URLWithString_(filename))

    panel.setAllowsMultipleSelection_(True)
    panel.setCanChooseDirectories_(True)

    if panel.runModal() != NSOKButton:
        return
    filenames = [unicodedata.normalize('NFC', file) for file in panel.filenames()]
    NSApp.delegate().contactsWindowController.sessionControllersManager.send_files_to_contact(account, dest_uri, filenames)


@implementer(IObserver)
class FileTransferWindowController(NSObject):

    window = objc.IBOutlet()
    listView = objc.IBOutlet()
    bottomLabel = objc.IBOutlet()
    transferSpeed = objc.IBOutlet()
    history = []
    loaded = False

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self):
        if self:
            notification_center = NotificationCenter()
            notification_center.add_observer(self, name="BlinkFileTransferNewOutgoing")
            notification_center.add_observer(self, name="BlinkFileTransferNewIncoming")
            notification_center.add_observer(self, name="BlinkFileTransferWillRestart")
            notification_center.add_observer(self, name="BlinkFileTransferDidEnd")
            notification_center.add_observer(self, name="BlinkFileTransferSpeedDidUpdate")
            notification_center.add_observer(self, name="BlinkShouldTerminate")

            NSBundle.loadNibNamed_owner_("FileTransferWindow", self)

            self.transferSpeed.setStringValue_('')

    @objc.python_method
    @run_in_green_thread
    def load_transfers_from_history(self):
        active_items = []
        for item in self.listView.subviews().copy():
            if item.done:
                item.removeFromSuperview()
            else:
                if item.transfer:
                    active_items.append(item.transfer.transfer_id)

        self.listView.relayout()
        self.listView.display()
        self.listView.setNeedsDisplay_(True)

        self.get_previous_transfers(active_items)
        
    @objc.python_method
    @run_in_green_thread
    def get_previous_transfers(self, active_items=()):
        results = FileTransferHistory().get_transfers(20)
        already_added_file = set()
        transfers = []
        for transfer in results:
            file_idx = '%s%s' % (transfer.file_path, transfer.remote_uri)

            if transfer.transfer_id in active_items:
                continue

            if file_idx in already_added_file:
                continue

            already_added_file.add(file_idx)
            transfers.append(transfer)

        self.render_previous_transfers(reversed(transfers))

    @objc.python_method
    @run_in_gui_thread
    def render_previous_transfers(self, transfers):
        last_displayed_item = self.listView.subviews().lastObject()

        for transfer in transfers:
            item = FileTransferItemView.alloc().initWithFrame_oldTransfer_(NSMakeRect(0, 0, 100, 100), transfer)
            if last_displayed_item:
                self.listView.insertItemView_before_(item, last_displayed_item)
            else:
                self.listView.addItemView_(item)

            self.listView.relayout()
            self.listView.display()
            h = self.listView.minimumHeight()
            self.listView.scrollRectToVisible_(NSMakeRect(0, h-1, 100, 1))

        count = len(self.listView.subviews())
        if count == 1:
            self.bottomLabel.setStringValue_(NSLocalizedString("1 item", "Label"))
        else:
            self.bottomLabel.setStringValue_(NSLocalizedString("%i items", "Label") % count if count else "")

        self.loaded = True

    @objc.python_method
    def refresh_transfer_rate(self):
        incoming_transfer_rate = 0
        outgoing_transfer_rate = 0
        for item in self.listView.subviews().copy():
            if item.transfer and item.transfer.transfer_rate is not None:
                if isinstance(item.transfer, IncomingFileTransferHandler):
                    incoming_transfer_rate += item.transfer.transfer_rate
                elif isinstance(item.transfer, OutgoingPushFileTransferHandler):
                    outgoing_transfer_rate += item.transfer.transfer_rate
                elif isinstance(item.transfer, OutgoingPullFileTransferHandler):
                    incoming_transfer_rate += item.transfer.transfer_rate

        if incoming_transfer_rate or outgoing_transfer_rate:
            if incoming_transfer_rate and outgoing_transfer_rate:
                f1 = format_size(incoming_transfer_rate, bits=True)
                f2 = format_size(outgoing_transfer_rate, bits=True)
                text = NSLocalizedString("Incoming %s/s", "Label") % f1 + ", " + NSLocalizedString("Outgoing %s/s", "Label") % f2
            elif incoming_transfer_rate:
                f = format_size(incoming_transfer_rate, bits=True)
                text = NSLocalizedString("Incoming %s/s", "Label") % f
            elif outgoing_transfer_rate:
                f = format_size(outgoing_transfer_rate, bits=True)
                text = NSLocalizedString("Outgoing %s/s", "Label") % f
            self.transferSpeed.setStringValue_(text)
        else:
            self.transferSpeed.setStringValue_('')

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    @objc.IBAction
    def close_(self, sender):
        self.window.close()

    @objc.python_method
    def _NH_BlinkShouldTerminate(self, sender, data):
        if self.window:
            self.window.orderOut_(self)

    @objc.IBAction
    def showWindow_(self, sender):
        if NSApp.delegate().contactsWindowController.sessionControllersManager.isMediaTypeSupported('file-transfer'):
            self.load_transfers_from_history()
            self.window.makeKeyAndOrderFront_(None)

    @objc.python_method
    def delete_history_transfers(self):
        return FileTransferHistory().delete_transfers()

    @objc.IBAction
    def clearList_(self, sender):
        self.delete_history_transfers()
        self.load_transfers_from_history()

    @objc.python_method
    def _NH_BlinkFileTransferWillRestart(self, sender, data):
        self.listView.relayout()

    @objc.python_method
    def _NH_BlinkFileTransferNewOutgoing(self, sender, data):
        try:
            item = next((item for item in self.listView.subviews().copy() if item.file_path == sender.ft_info.file_path and item.remote_uri == sender.ft_info.remote_uri))
            item.replaceWithTransfer_(sender)
            self.listView.relayout()

        except StopIteration:
            item = FileTransferItemView.alloc().initWithFrame_transfer_(NSMakeRect(0, 0, 100, 100), sender)
            self.listView.addItemView_(item)
            h = NSHeight(self.listView.frame())
            self.listView.scrollRectToVisible_(NSMakeRect(0, h-1, 100, 1))


        file_path = sender.ft_info.file_path.decode() if isinstance(sender.ft_info.file_path, bytes) else sender.ft_info.file_path
        if 'screencapture' not in file_path:
            self.window.orderFront_(None)

        count = len(self.listView.subviews())
        if count == 1:
            self.bottomLabel.setStringValue_(NSLocalizedString("1 item", "Label"))
        else:
            self.bottomLabel.setStringValue_(NSLocalizedString("%i items", "Label") % count)

    _NH_BlinkFileTransferNewIncoming = _NH_BlinkFileTransferNewOutgoing

    @objc.python_method
    def _NH_BlinkFileTransferSpeedDidUpdate(self, sender, data):
        self.refresh_transfer_rate()

    @objc.python_method
    def _NH_BlinkFileTransferDidEnd(self, sender, data):
        self.listView.relayout()
        self.refresh_transfer_rate()
        if not data.error:
            # jump dock icon and bring window to front
            if isinstance(sender, IncomingFileTransferHandler):
                self.window.orderFront_(None)
                NSApp.requestUserAttention_(NSInformationalRequest)
            elif 'screencapture' not in sender.file_path:
                self.window.orderFront_(None)
                NSApp.requestUserAttention_(NSInformationalRequest)

