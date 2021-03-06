# Copyright (C) 2013 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSOKButton, NSCancelButton, NSOnState, NSControlTextDidChangeNotification
from Foundation import NSObject, NSBundle, NSColor, NSLocalizedString, NSTimer, NSRunLoop, NSRunLoopCommonModes, NSNotificationCenter

import objc

import os
import shutil

from application.system import makedirs
from sipsimple.streams.msrp.chat import ChatStreamError
from sipsimple.util import ISOTimestamp
from util import format_identity_to_string

from resources import ApplicationData

from BlinkLogger import BlinkLogger


class ChatOtrSmp(NSObject):
    window = objc.IBOutlet()
    labelText = objc.IBOutlet()
    secretText = objc.IBOutlet()
    questionText = objc.IBOutlet()
    progressBar = objc.IBOutlet()
    statusText = objc.IBOutlet()
    continueButton = objc.IBOutlet()
    cancelButton = objc.IBOutlet()

    finished = False
    requested_by_remote = None
    timer = None
    smp_running = False

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, controller):
        NSBundle.loadNibNamed_owner_("ChatOtrSmp", self)
        self.controller = controller
        self.statusText.setStringValue_('')
        self.progressBar.startAnimation_(None)
        self.window.setTitle_(NSLocalizedString("Identity Verification for %s", "Window title") % self.controller.sessionController.titleShort)
        self.stream = self.controller.stream
        self.remote_address = self.controller.sessionController.remoteAOR
    
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "controlTextDidChange:", NSControlTextDidChangeNotification, self.secretText)

    def close(self):
        self.controller = None
        if self.timer is not None:
            if self.timer.isValid():
                self.timer.invalidate()
            self.timer = None
        self.release()

    def dealloc(self):
        objc.super(ChatOtrSmp, self).dealloc()

    @objc.IBAction
    def okClicked_(self, sender):
        self.statusText.setTextColor_(NSColor.blackColor())
        if self.finished:
            self.window.orderOut_(self)
            return

        secret = self.secretText.stringValue()
        secret = secret.encode() if secret else None

        if self.requested_by_remote:
            self.controller.sessionController.log_info("OTR SMP verification will be answered")
            self.stream.encryption.smp_answer(secret)
            self.smp_running = True
            self.progressBar.setDoubleValue_(6)
        else:
            qtext = self.questionText.stringValue()
            qtext = qtext.encode() if qtext else None
            self.controller.sessionController.log_info("OTR SMP verification will be requested")
            self.stream.encryption.smp_verify(secret, qtext)
            self.progressBar.setIndeterminate_(False)
            self.smp_running = True
            self.progressBar.setDoubleValue_(3)
            self.statusText.setStringValue_(NSLocalizedString("Verification request sent", "Label"))
            self.continueButton.setEnabled_(False)

    @objc.IBAction
    def secretEntered_(self, sender):
        self.okClicked_(None)

    def controlTextDidChange_(self, notification):
        secret = self.secretText.stringValue().encode('utf-8')
        self.continueButton.setEnabled_(bool(secret))

    @objc.IBAction
    def cancelClicked_(self, sender):
        self.window.orderOut_(self)
        if self.smp_running:
            self.controller.sessionController.log_info("OTR SMP verification will be aborted")
            self.stream.encryption.smp_abort()
            self.smp_running = False

    def handle_remote_response(self, same_secrets=False):
        if not same_secrets:
            self.statusText.setTextColor_(NSColor.redColor())
            self.statusText.setStringValue_(NSLocalizedString("Identity verification failed. Try again later.", "Label"))
            result = False
        else:
            self.stream.encryption.verified = True
            self.statusText.setTextColor_(NSColor.greenColor())
            self.statusText.setStringValue_(NSLocalizedString("Identity verification succeeded", "Label"))
            self.controller.revalidateToolbar()
            self.controller.updateEncryptionWidgets()
            result = True

        self._finish(result)
        return result

    def _finish(self, result=False):
        self.finished = True
        self.smp_running = False
        self.requested_by_remote = False
        self.secretText.setEnabled_(False)
        self.questionText.setEnabled_(False)
        self.progressBar.setDoubleValue_(9)
        self.continueButton.setEnabled_(False)
        self.continueButton.setTitle_(NSLocalizedString("Finish", "Button Title"))
        self.cancelButton.setHidden_(True)
        self.continueButton.setEnabled_(True)
        wait_interval = 5 if result else 10
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(wait_interval, self, "verificationFinished:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)

    def verificationFinished_(self, timer):
        self.okClicked_(None)

    def show(self, question=None, remote=False):
        self.smp_running = True
        self.finished = False

        if remote:
            self.requested_by_remote = True
            self.statusText.setStringValue_(NSLocalizedString("Identity verification request received", "Label"))

        self.secretText.setEnabled_(True)
        self.questionText.setEnabled_(True)
        self.cancelButton.setHidden_(False)
        self.continueButton.setTitle_(NSLocalizedString("Continue", "Button title"))
        self.progressBar.setIndeterminate_(False)
        self.progressBar.setDoubleValue_(3 if self.requested_by_remote else 0)

        self.secretText.setStringValue_('')

        self.window.makeKeyAndOrderFront_(None)

        if self.requested_by_remote:
            self.continueButton.setTitle_(NSLocalizedString("Respond", "Button title"))
            self.progressBar.setIndeterminate_(False)
            self.progressBar.setDoubleValue_(3)
            if question is None:
                self.questionText.setHidden_(True)
                self.labelText.setStringValue_(NSLocalizedString("%s wants to verify your identity using a commonly known secret.", "Label") % self.remote_address)
            else:
                self.questionText.setHidden_(False)
                self.secretText.setHidden_(False)
                self.questionText.setStringValue_(question.decode())
                self.questionText.setEnabled_(False)
                self.labelText.setStringValue_(NSLocalizedString("%s has asked you a question to verify your identity:", "Label") % self.remote_address)
        else:
            self.statusText.setStringValue_('')
            self.questionText.setHidden_(False)
            self.questionText.setStringValue_('')
            self.questionText.setEnabled_(True)
            self.labelText.setStringValue_(NSLocalizedString("You want to verify the identity of %s using a commonly known secret. Optionally, you can ask a question as a hint.", "Label") % self.remote_address)

