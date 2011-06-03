# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

from AppKit import *
from Foundation import *

import os

from application.notification import NotificationCenter, IObserver
from application.python import Null
from zope.interface import implements

from FileTransferSession import OutgoingPushFileTransferHandler
from util import allocate_autorelease_pool, format_size, run_in_gui_thread


class FileTransferItemView(NSView):
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

    checksumProgressBar = objc.IBOutlet()

    failed = False
    done = False

    transfer = None
    oldTransferInfo = None


    def initWithFrame_oldTransfer_(self, frame, transferInfo):
        self = NSView.initWithFrame_(self, frame)
        if self:
            self.oldTransferInfo = transferInfo

            NSBundle.loadNibNamed_owner_("FileTransferItemView", self)

            filename = transferInfo.file_path
            if filename.endswith(".download"):
                filename = filename[:-len(".download")]

            self.updateIcon(NSWorkspace.sharedWorkspace().iconForFile_(filename))

            self.nameText.setStringValue_(os.path.basename(filename))
            self.fromText.setStringValue_('To %s' % transferInfo.remote_uri if transferInfo.direction=='outgoing' else 'From %s' % transferInfo.remote_uri)

            if transferInfo.status == "completed":
                status = "%s %s Completed"%(format_size(transferInfo.file_size, 1024), unichr(0x2014))
            else:
                error = transferInfo.status

                if transferInfo.direction == "outgoing":
                    status = error
                else:
                    status = "%s of %s"%(format_size(transferInfo.bytes_transfered, 1024), format_size(transferInfo.file_size, 1024))
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

            NSBundle.loadNibNamed_owner_("FileTransferItemView", self)

            filename = self.transfer.file_path

            if type(self.transfer) == OutgoingPushFileTransferHandler:
                self.fromText.setStringValue_(u"To:  %s" % self.transfer.account.id)
            else:
                if filename.endswith(".download"):
                    filename = filename[:-len(".download")]
                self.fromText.setStringValue_(u"From:  %s" % self.transfer.account.id)
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
            self.progressBar.setHidden_(True)

            self.checksumProgressBar.setIndeterminate_(False)
            self.checksumProgressBar.startAnimation_(None)
            self.checksumProgressBar.setHidden_(False)

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

        # overlay file transfer direction icon 
        if type(self.transfer) == OutgoingPushFileTransferHandler or (self.oldTransferInfo and self.oldTransferInfo.direction == "outgoing"):
            icon = NSImage.imageNamed_("outgoing_file")
        else:
            icon = NSImage.imageNamed_("incoming_file")
        icon.drawAtPoint_fromRect_operation_fraction_(NSMakePoint(2, 4), NSMakeRect(0, 0, size.width, size.height), NSCompositeSourceOver, 1)
        image.unlockFocus()

        self.icon.setImage_(image)

    def relayoutForDone(self):
        self.progressBar.setHidden_(True)
        self.checksumProgressBar.setHidden_(True)
        self.stopButton.setHidden_(True)
        frame = self.frame()
        frame.size.height = 52
        self.setFrame_(frame)

    def relayoutForRetry(self):

        self.stopButton.setHidden_(False)
        self.retryButton.setHidden_(True)

        self.progressBar.setHidden_(True)

        self.checksumProgressBar.setHidden_(False)
        self.checksumProgressBar.setIndeterminate_(False)
        self.checksumProgressBar.setDoubleValue_(0)

        frame = self.frame()
        frame.size.height = self.originalHeight
        self.setFrame_(frame)

    @allocate_autorelease_pool
    @run_in_gui_thread
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
            NSWorkspace.sharedWorkspace().openFile_(self.oldTransferInfo.file_path)

    @objc.IBAction
    def stopTransfer_(self, sender):
        self.transfer.cancel()

    @objc.IBAction
    def retryTransfer_(self, sender):
        self.failed = False
        self.done = False

        self.updateProgressInfo()
        self.progressBar.setIndeterminate_(True)
        self.progressBar.startAnimation_(None)
        self.progressBar.setHidden_(True)

        self.updateChecksumProgressInfo(0)
        self.checksumProgressBar.setIndeterminate_(False)
        self.checksumProgressBar.startAnimation_(None)
        self.checksumProgressBar.setHidden_(False)

        self.sizeText.setTextColor_(NSColor.grayColor())
        self.relayoutForRetry()
        self.transfer.retry()

    @objc.IBAction
    def revealFile_(self, sender):
        if self.transfer and self.transfer.file_path:
            path = self.transfer.file_path
        elif self.oldTransferInfo:
            path = self.oldTransferInfo.file_path
        else:
            return

        dirname = os.path.dirname(path)
        NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(path, dirname)

    def _NH_BlinkFileTransferInitiated(self, notification):
        if not self.failed:
            self.sizeText.setStringValue_(self.transfer.status)
            self.progressBar.setHidden_(False)
            self.checksumProgressBar.setHidden_(True)
        else:
            self.sizeText.setStringValue_('File Transfer aborted')

    def _NH_BlinkFileTransferRestarting(self, notification):
        self.sizeText.setStringValue_(self.transfer.status)

    def _NH_BlinkFileTransferDidStart(self, notification):
        self.progressBar.setIndeterminate_(False)

    def _NH_BlinkFileTransferDidEnd(self, notification):
        self.sizeText.setTextColor_(NSColor.blueColor())
        self.progressBar.stopAnimation_(None)
        self.updateProgressInfo()
        self.relayoutForDone()
        self.done = True
        self.failed = False

    def _NH_BlinkFileTransferDidFail(self, notification):
        self.sizeText.setTextColor_(NSColor.redColor())

        self.checksumProgressBar.setHidden_(True)
        self.checksumProgressBar.stopAnimation_(None)
        self.progressBar.setHidden_(True)
        self.progressBar.stopAnimation_(None)
        self.updateProgressInfo()

        self.stopButton.setHidden_(True)
        if type(self.transfer) == OutgoingPushFileTransferHandler:
            self.retryButton.setHidden_(False)
        self.relayoutForDone()
        self.done = True
        self.failed = True

    def _NH_BlinkFileTransferUpdate(self, notification):
        self.updateProgressInfo()

    def _NH_BlinkFileTransferHashUpdate(self, notification):
        self.updateChecksumProgressInfo(notification.data.progress)

    def _NH_BlinkFileTransferDidComputeHash(self, notification):
        pass

    def updateProgressInfo(self):
        self.fromText.setStringValue_(self.transfer.target_text)
        self.sizeText.setStringValue_(self.transfer.progress_text)
        self.progressBar.setDoubleValue_(self.transfer.progress*100)

    def updateChecksumProgressInfo(self, progress):
        self.checksumProgressBar.setDoubleValue_(progress)
        self.sizeText.setStringValue_('Calculating checksum: %s%%' % progress)

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


