# Copyright (C) 2012 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSCancelButton, NSOKButton
from Foundation import NSBundle, NSObject
import objc


class SubjectController(NSObject):
    window = objc.IBOutlet()
    caption = objc.IBOutlet()
    nameText = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        NSBundle.loadNibNamed_owner_("Subject", self)
        return self

    def runModal(self, subject=''):
        self.window.makeKeyAndOrderFront_(None)
        if subject is not None:
            self.nameText.setStringValue_(subject)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            return str(self.nameText.stringValue())
        return None

    @objc.IBAction
    def okClicked_(self, sender):
        NSApp.stopModalWithCode_(NSOKButton)

    @objc.IBAction
    def cancelClicked_(self, sender):
        NSApp.stopModalWithCode_(NSCancelButton)

    def windowShouldClose_(self, sender):
        NSApp.stopModalWithCode_(NSCancelButton)
        return True

