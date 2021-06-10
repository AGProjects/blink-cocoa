# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import NSCompositeSourceOver, NSProcessInfo
from AppKit import NSApp

from Foundation import (NSBundle,
                        NSColor,
                        NSHeight,
                        NSImage,
                        NSLocalizedString,
                        NSMakePoint,
                        NSMakeRect,
                        NSMakeSize,
                        NSView,
                        NSWorkspace,
                        NSDownloadsDirectory, NSSearchPathForDirectoriesInDomains, NSUserDomainMask
                        )
import objc

import os
import unicodedata

from application.notification import NotificationCenter, IObserver
from application.python import Null
from application.system import makedirs, unlink
from zope.interface import implementer

from sipsimple.account import AccountManager
from resources import ApplicationData
from FileTransferSession import OutgoingPushFileTransferHandler
from util import format_size, format_date, run_in_gui_thread, normalize_sip_uri_for_outgoing_session


@implementer(IObserver)
class FileTransferItemView(NSView):

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
    file_path = None
    local_uri = None
    remote_uri = None

    transfer = None
    oldTransferInfo = None

    def initWithFrame_oldTransfer_(self, frame, transferInfo):
        self = NSView.initWithFrame_(self, frame)
        if self:
            self.oldTransferInfo = transferInfo
            self.file_path = transferInfo.file_path
            self.remote_uri = transferInfo.remote_uri
            self.local_uri = transferInfo.local_uri

            NSBundle.loadNibNamed_owner_("FileTransferItemView", self)
            self.updateIcon(NSWorkspace.sharedWorkspace().iconForFile_(self.file_path))

            self.nameText.setStringValue_(os.path.basename(self.file_path))
            self.fromText.setStringValue_('To %s from account %s' % (transferInfo.remote_uri, transferInfo.local_uri) if transferInfo.direction=='outgoing' else 'From %s to account %s' % (transferInfo.remote_uri, transferInfo.local_uri))
            
            self.revealButton.setHidden_(not os.path.exists(self.file_path))

            time_print = format_date(transferInfo.time)
            if transferInfo.status == "completed":
                self.sizeText.setTextColor_(NSColor.blueColor())
                t = NSLocalizedString("Completed transfer of ", "Label")
                status = t + "%s %s" % (format_size(transferInfo.file_size, 1024), time_print)
            else:
                self.sizeText.setTextColor_(NSColor.redColor())
                status = "%s %s" % (transferInfo.status.title(), time_print)

            self.sizeText.setStringValue_(status)
            frame.size = self.view.frame().size
            self.setFrame_(frame)
            self.addSubview_(self.view)
            self.relayoutForDone()
            if transferInfo.direction == "outgoing" and transferInfo.status != "completed" and os.path.exists(self.file_path):
                self.retryButton.setHidden_(False)
            self.done = True
        return self

    def replaceWithTransfer_(self, transfer):
        self.transfer = transfer
        NotificationCenter().add_observer(self, sender=transfer)
        self.stopButton.setHidden_(False)
        self.retryButton.setHidden_(True)
        self.progressBar.setHidden_(True)
        self.checksumProgressBar.setHidden_(False)
        self.checksumProgressBar.setIndeterminate_(False)
        self.checksumProgressBar.startAnimation_(None)
        self.sizeText.setTextColor_(NSColor.grayColor())

        frame = self.frame()
        frame.size.height = 68
        self.setFrame_(frame)
        self.addSubview_(self.view)

    def initWithFrame_transfer_(self, frame, transfer):
        self = NSView.initWithFrame_(self, frame)
        if self:
            self.transfer = transfer
            NotificationCenter().add_observer(self, sender=transfer)

            NSBundle.loadNibNamed_owner_("FileTransferItemView", self)

            self.file_path = os.path.basename(self.transfer.ft_info.file_path)
            self.nameText.setStringValue_(self.file_path)
            self.remote_uri = self.transfer.ft_info.remote_uri
            self.local_uri = self.transfer.ft_info.local_uri

            if type(self.transfer) == OutgoingPushFileTransferHandler:
                self.fromText.setStringValue_("To:  %s" % self.transfer.account.id)
            else:
                self.fromText.setStringValue_("From:  %s" % self.transfer.account.id)
            self.revealButton.setHidden_(False)

            # XXX: there should be a better way to do this!
            tmp_folder = ApplicationData.get('.tmp_file_transfers')
            makedirs(tmp_folder, 0o700)
            tmpf = tmp_folder + "/tmpf-" + (self.file_path.decode() if isinstance(self.file_path, bytes) else self.file_path)
            with open(tmpf, "wb+"):
                self.updateIcon(NSWorkspace.sharedWorkspace().iconForFile_(tmpf))
            unlink(tmpf)

            self.updateProgressInfo()
            self.progressBar.setIndeterminate_(True)
            self.progressBar.startAnimation_(None)

            self.checksumProgressBar.setIndeterminate_(False)
            self.checksumProgressBar.startAnimation_(None)

            if transfer.direction == 'outgoing':
                self.progressBar.setHidden_(True)
                self.checksumProgressBar.setHidden_(False)
            else:
                self.progressBar.setHidden_(False)
                self.checksumProgressBar.setHidden_(True)

            frame.size = self.view.frame().size
            self.setFrame_(frame)
            self.addSubview_(self.view)
            self.originalHeight = NSHeight(frame)
        return self

    def dealloc(self):
        objc.super(FileTransferItemView, self).dealloc()

    @objc.python_method
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

    @objc.python_method
    def relayoutForDone(self):
        self.progressBar.stopAnimation_(None)
        self.progressBar.setHidden_(True)
        self.checksumProgressBar.stopAnimation_(None)
        self.checksumProgressBar.setHidden_(True)
        self.stopButton.setHidden_(True)
        frame = self.frame()
        frame.size.height = 52
        self.setFrame_(frame)

    @objc.python_method
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

    @objc.python_method
    def updateProgressInfo(self):
        self.fromText.setStringValue_(self.transfer.target_text)
        self.sizeText.setStringValue_(self.transfer.progress_text)

    @objc.python_method
    def updateChecksumProgressInfo(self, progress):
        self.checksumProgressBar.setDoubleValue_(progress)
        self.sizeText.setStringValue_('Calculating checksum: %d%%' % progress)

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
        self.transfer.end()

    @objc.IBAction
    def retryTransfer_(self, sender):
        if self.oldTransferInfo:
            try:
                account = next((account for account in AccountManager().iter_accounts() if account.id == self.oldTransferInfo.local_uri))
            except StopIteration:
                account = AccountManager().default_account
            target_uri = normalize_sip_uri_for_outgoing_session(self.oldTransferInfo.remote_uri, AccountManager().default_account)
            filenames = [unicodedata.normalize('NFC', self.oldTransferInfo.file_path)]
            NSApp.delegate().contactsWindowController.sessionControllersManager.send_files_to_contact(account, target_uri, filenames)
        else:
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
        if self.transfer and self.transfer.ft_info and self.transfer.ft_info.file_path:
            path = self.transfer.ft_info.file_path
        elif self.oldTransferInfo:
            environ = NSProcessInfo.processInfo().environment()
            inSandbox = environ.objectForKey_("APP_SANDBOX_CONTAINER_ID")
            if inSandbox is not None:
                download_folder = unicodedata.normalize('NFC', NSSearchPathForDirectoriesInDomains(NSDownloadsDirectory, NSUserDomainMask, True)[0])
                NSWorkspace.sharedWorkspace().openFile_(download_folder)
                return
            path = self.oldTransferInfo.file_path
        else:
            return

        dirname = os.path.dirname(path)
        NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(path, dirname)

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_BlinkFileTransferDidInitialize(self, notification):
        self.sizeText.setStringValue_(self.transfer.status)
        self.progressBar.setHidden_(False)
        self.checksumProgressBar.setHidden_(True)

    @objc.python_method
    def _NH_BlinkFileTransferWillRestart(self, notification):
        self.sizeText.setStringValue_(self.transfer.status)

    @objc.python_method
    def _NH_BlinkFileTransferDidStart(self, notification):
        self.progressBar.setIndeterminate_(False)
        # update path
        self.nameText.setStringValue_(os.path.basename(self.transfer.file_path))

    @objc.python_method
    def _NH_BlinkFileTransferDidEnd(self, notification):
        if notification.data.error:
            self.sizeText.setTextColor_(NSColor.redColor())
            if type(self.transfer) == OutgoingPushFileTransferHandler:
                self.retryButton.setHidden_(False)
        else:
            self.sizeText.setTextColor_(NSColor.blueColor())
            if self.transfer.direction == 'incoming':
                self.revealButton.setHidden_(False)
        self.fromText.setStringValue_(self.transfer.target_text)
        self.sizeText.setStringValue_(self.transfer.progress_text)
        self.failed = notification.data.error
        self.done = True
        self.relayoutForDone()

    @objc.python_method
    def _NH_BlinkFileTransferProgress(self, notification):
        self.fromText.setStringValue_(self.transfer.target_text)
        self.sizeText.setStringValue_(self.transfer.progress_text)
        self.progressBar.setDoubleValue_(notification.data.progress)

    @objc.python_method
    def _NH_BlinkFileTransferHashProgress(self, notification):
        self.updateChecksumProgressInfo(notification.data.progress)

