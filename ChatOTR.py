# Copyright (C) 2013 AG Projects. See LICENSE for details.
#


from AppKit import NSApp, NSOKButton, NSCancelButton, NSOnState
from Foundation import NSObject, NSBundle, NSColor, NSLocalizedString, NSTimer, NSRunLoop, NSRunLoopCommonModes

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
    response = None
    question = None
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

    def close(self):
        self.controller = None
        if self.timer is not None:
            if self.timer.isValid():
                self.timer.invalidate()
            self.timer = None
        self.release()

    def dealloc(self):
        super(ChatOtrSmp, self).dealloc()

    @objc.IBAction
    def okClicked_(self, sender):
        self.statusText.setTextColor_(NSColor.blackColor())
        if self.finished:
            self.window.orderOut_(self)
            return

        secret = self.secretText.stringValue().encode('utf-8')

        if self.response:
            if not secret:
                if self.smp_running:
                    self.controller.sessionController.log_info(u"SMP verification will be aborted")
                    self.stream.encryption.smp_abort()
            else:
                self.controller.sessionController.log_info(u"SMP verification will be answered")
                self.stream.encryption.smp_answer(secret)
                self.smp_running = True
        else:
            if not secret:
                if self.smp_running:
                    self.controller.sessionController.log_info(u"SMP verification will be aborted because no secret was entered")
                    self.stream.encryption.smp_abort()
                self._finish()
            else:
                qtext = self.questionText.stringValue()
                self.controller.sessionController.log_info(u"SMP verification will be requested")
                self.stream.encryption.smp_verify(secret, qtext.encode('utf-8') if qtext else None)
                self.progressBar.setIndeterminate_(False)
                self.smp_running = True
                self.progressBar.setDoubleValue_(3)
                self.statusText.setStringValue_(NSLocalizedString("Verification request sent", "Label"))
                self.continueButton.setEnabled_(False)
            # self.statusText.setStringValue_(NSLocalizedString("Chat session is not OTR encrypted", "Label"))
            # self.statusText.setStringValue_(NSLocalizedString("OTR encryption error: %s", "Label") % e)
            # self.statusText.setStringValue_(NSLocalizedString("Error: %s", "Label") % e)

    @objc.IBAction
    def cancelClicked_(self, sender):
        self.window.orderOut_(self)
        if self.smp_running:
            self.controller.sessionController.log_info(u"SMP verification will be aborted")
            self.stream.encryption.smp_abort()
            self.smp_running = False

    def handle_remote_response(self, same_secrets=False):
        if not same_secrets:
            self.statusText.setTextColor_(NSColor.redColor())
            self.statusText.setStringValue_(NSLocalizedString("Identity verification failed. Try again later.", "Label"))
        else:
            self.stream.encryption.verified = True
            self.statusText.setTextColor_(NSColor.greenColor())
            self.statusText.setStringValue_(NSLocalizedString("Identity verification succeeded", "Label"))
            self.controller.revalidateToolbar()
            self.controller.updateEncryptionWidgets()

        self._finish()

    def _finish(self):
        self.finished = True
        self.smp_running = False
        self.secretText.setEnabled_(False)
        self.questionText.setEnabled_(False)
        self.progressBar.setDoubleValue_(9)
        self.continueButton.setEnabled_(True)
        self.continueButton.setTitle_(NSLocalizedString("Finish", "Button Title"))
        self.cancelButton.setHidden_(True)
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(5, self, "verificationFinished:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)

    def verificationFinished_(self, timer):
        self.okClicked_(None)

    def show(self, question=None):
        if question:
            self.response = True
            self.smp_running = True
            self.statusText.setStringValue_(NSLocalizedString("Identity verification request received", "Label"))

        self.question = question
        self.secretText.setEnabled_(True)
        self.questionText.setEnabled_(True)
        self.cancelButton.setHidden_(False)
        self.continueButton.setTitle_(NSLocalizedString("Continue", "Button title"))
        self.progressBar.setIndeterminate_(False)
        self.progressBar.setDoubleValue_(0)

        self.finished = False

        self.secretText.setStringValue_('')

        self.window.makeKeyAndOrderFront_(None)

        if self.response:
            self.continueButton.setTitle_(NSLocalizedString("Respond", "Button title"))
            self.progressBar.setIndeterminate_(True)
            self.continueButton.setEnabled_(True)
            if self.question is None:
                self.questionText.setHidden_(True)
                self.labelText.setStringValue_(NSLocalizedString("%s wants to verify your identity using a commonly known secret.", "Label") % self.remote_address)
            else:
                self.questionText.setHidden_(False)
                self.secretText.setHidden_(False)
                self.questionText.setStringValue_(self.question)
                self.questionText.setEnabled_(False)
                self.labelText.setStringValue_(NSLocalizedString("%s has asked you a question to verify your identity:", "Label") % self.remote_address)
        else:
            self.statusText.setStringValue_('')
            self.continueButton.setEnabled_(True)
            self.questionText.setHidden_(False)
            self.questionText.setStringValue_('')
            self.questionText.setEnabled_(True)
            self.labelText.setStringValue_(NSLocalizedString("You want to verify the identity of %s using a commonly known secret. Optionally, you can ask a question as a hint.", "Label") % self.remote_address)

