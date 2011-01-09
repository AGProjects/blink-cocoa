# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

from AppKit import *
from Foundation import *

import os

from application.notification import NotificationCenter, IObserver
from application.python.util import Null
from zope.interface import implements

from BlinkLogger import BlinkLogger
from FileTransferSession import OutgoingFileTransfer
from util import allocate_autorelease_pool, format_size


class FileTransferItem(NSView):
    implements(IObserver)
    
    view = objc.IBOutlet()
    icon = objc.IBOutlet()
    nameText = objc.IBOutlet()
    fromText = objc.IBOutlet()
    sizeText = objc.IBOutlet()
    revealButton = objc.IBOutlet()

    stopButton = objc.IBOutlet()
    progressBar = objc.IBOutlet()
    retryButton = objc.IBOutlet()

    failed = False
    done = False

    transfer = None
    oldTransferInfo = None


    def initWithFrame_oldTransfer_(self, frame, transferInfo):
        self = NSView.initWithFrame_(self, frame)
        if self:
            self.oldTransferInfo = transferInfo

            NSBundle.loadNibNamed_owner_("FileTransferItem", self)

            filename = transferInfo["path"]
            if filename.endswith(".download"):
                filename = filename[:-len(".download")]

            self.updateIcon(NSWorkspace.sharedWorkspace().iconForFile_(filename))

            self.nameText.setStringValue_(os.path.basename(filename))
            self.fromText.setStringValue_(transferInfo["peer"])

            if transferInfo["status"] == "done":
                status = "%s %s Done"%(format_size(transferInfo["bytes_total"], 1024), unichr(0x2014))
            else:
                if transferInfo["status"].startswith("failed:"):
                    error = transferInfo["status"][len("failed:"):]
                else:
                    error = "failed"

                if transferInfo["direction"] == "send":
                    status = error
                else:
                    status = "%s of %s"%(format_size(transferInfo["bytes_transfered"], 1024), format_size(transferInfo["bytes_total"], 1024))
                    status = "%s %s %s"%(status, unichr(0x2014), error)
            self.sizeText.setStringValue_(status)
            frame.size = self.view.frame().size
            self.setFrame_(frame)
            self.addSubview_(self.view)
            self.relayoutForDone()
            self.done = True
        return self

    def initWithFrame_transfer_(self, frame, transfer):
        self = NSView.initWithFrame_(self, frame)
        if self:
            self.transfer = transfer
            NotificationCenter().add_observer(self, sender=transfer)

            NSBundle.loadNibNamed_owner_("FileTransferItem", self)

            filename = self.transfer.file_path

            if type(self.transfer) == OutgoingFileTransfer:
                self.fromText.setStringValue_(u"To:  %s" % self.transfer.session.remote_identity)
            else:
                if filename.endswith(".download"):
                    filename = filename[:-len(".download")]
                self.fromText.setStringValue_(u"From:  %s" % self.transfer.session.remote_identity)
            self.nameText.setStringValue_(os.path.basename(filename))

            if os.path.exists(filename):
                self.updateIcon(NSWorkspace.sharedWorkspace().iconForFile_(filename))
            else:
                tmpf = "/tmp/tmpf"+os.path.splitext(filename)[1]
                open(tmpf, "w+").close()
                self.updateIcon(NSWorkspace.sharedWorkspace().iconForFile_(tmpf))
                os.remove(tmpf)
            self.updateProgressInfo()
            self.progressBar.setIndeterminate_(True)
            self.progressBar.startAnimation_(None)
            frame.size = self.view.frame().size
            self.setFrame_(frame)
            self.addSubview_(self.view)
            self.originalHeight = NSHeight(frame)
        return self

    def updateIcon(self, icon):
        image = NSImage.alloc().initWithSize_(NSMakeSize(48,48))
        image.lockFocus()
        size = icon.size()
        icon.drawAtPoint_fromRect_operation_fraction_(NSMakePoint((48-size.width)/2, (48-size.height)/2), 
                NSMakeRect(0, 0, size.width, size.height), NSCompositeSourceOver, 1)

        if type(self.transfer) == OutgoingFileTransfer or (self.oldTransferInfo and self.oldTransferInfo["direction"] == "send"):
            icon = NSImage.imageNamed_("upfile")
        else:
            icon = NSImage.imageNamed_("downfile")
        icon.drawAtPoint_fromRect_operation_fraction_(NSMakePoint(8, 4),
            NSMakeRect(0, 0, size.width, size.height), NSCompositeSourceOver, 1)
        image.unlockFocus()

        self.icon.setImage_(image)

    def relayoutForDone(self):
        self.progressBar.setHidden_(True)
        self.stopButton.setHidden_(True)
        frame = self.frame()
        frame.size.height = 52
        self.setFrame_(frame)

    def relayoutForRetry(self):
        self.progressBar.setHidden_(False)
        self.stopButton.setHidden_(False)
        self.retryButton.setHidden_(True)
        frame = self.frame()
        frame.size.height = self.originalHeight
        self.setFrame_(frame)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def setSelected_(self, flag):
        if flag:
            self.nameText.setTextColor_(NSColor.whiteColor())
            self.fromText.setTextColor_(NSColor.whiteColor())
            self.sizeText.setTextColor_(NSColor.whiteColor())
        else:
            self.nameText.setTextColor_(NSColor.blackColor())
            self.fromText.setTextColor_(NSColor.grayColor())
            if self.failed:
                self.sizeText.setTextColor_(NSColor.redColor())
            else:
                self.sizeText.setTextColor_(NSColor.grayColor())

    def mouseDown_(self, event):
        if event.clickCount() == 2:
            self.activateFile_(None)
        else:
            NSView.mouseDown_(self, event)

    @objc.IBAction
    def activateFile_(self, sender):
        if self.transfer:
            NSWorkspace.sharedWorkspace().openFile_(self.transfer.file_path)
        elif self.oldTransferInfo:
            NSWorkspace.sharedWorkspace().openFile_(self.oldTransferInfo["path"])

    @objc.IBAction
    def stopTransfer_(self, sender):
        self.transfer.cancel()

    @objc.IBAction
    def retryTransfer_(self, sender):
        self.failed = False
        self.done = False
        self.progressBar.setIndeterminate_(True)
        self.progressBar.startAnimation_(None)
        self.sizeText.setTextColor_(NSColor.grayColor())
        self.relayoutForRetry()
        try:
            self.transfer.retry()
        except Exception, exc:
            import traceback
            traceback.print_exc()
            BlinkLogger().log_error("Error while attempting to resume file transfer: %s"%exc)
            self._NH_BlinkFileTransferDidFail(None, None)
            self.sizeText.setStringValue_("Error: %s" % exc)
            return
        self.updateProgressInfo()

    @objc.IBAction
    def revealFile_(self, sender):
        if self.transfer and self.transfer.file_path:
            path = self.transfer.file_path
        elif self.oldTransferInfo:
            path = self.oldTransferInfo["path"]
        else:
            return

        dirname = os.path.dirname(path)
        NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(path, dirname)

    def _NH_BlinkFileTransferDidStart(self, notification):
        self.progressBar.setIndeterminate_(False)

    def _NH_BlinkFileTransferDidEnd(self, notification):
        self.progressBar.stopAnimation_(None)
        self.updateProgressInfo()
        self.relayoutForDone()
        self.done = True
        self.failed = False

    def _NH_BlinkFileTransferDidFail(self, notification):
        self.sizeText.setTextColor_(NSColor.redColor())
        self.progressBar.setHidden_(True)
        self.progressBar.stopAnimation_(None)
        self.updateProgressInfo()
        self.stopButton.setHidden_(True)
        if type(self.transfer) == OutgoingFileTransfer:
            self.retryButton.setHidden_(False)
        self.relayoutForDone()
        self.done = True
        self.failed = True

    def _NH_BlinkFileTransferUpdate(self, notification):
        self.updateProgressInfo()

    def updateProgressInfo(self):
        self.fromText.setStringValue_(self.transfer.target_text)
        self.sizeText.setStringValue_(self.transfer.progress_text)
        self.progressBar.setDoubleValue_(self.transfer.progress*100)

    def setFileInfo(self, info):
        assert type(info) == dict
        assert set(info.keys()) == set(["upload", "size", "total", "peer", "path"])

        self.transfer = None
        self.fileInfo = info

        NSBundle.loadNibNamed_owner_("FileTransferItemDone", self)

        filename = os.path.basename(info["path"])
        if filename.endswith(".download"):
            filename = filename[:-len(".download")]
        self.nameText.setStringValue_(filename)
        self.fromText.setStringValue_("%s  %s"%("From: " if info["upload"] else "To: ", info["peer"]))
        self.sizeText.setStringValue_(format_size(info["size"]))


