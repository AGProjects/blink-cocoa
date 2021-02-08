# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSCancelButton, NSOKButton
from Foundation import NSBundle, NSObject, NSLocalizedString
import objc


class AddGroupController(NSObject):
    window = objc.IBOutlet()
    caption = objc.IBOutlet()
    nameText = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        NSBundle.loadNibNamed_owner_("Group", self)
        return self

    def runModal(self):
        self.window.makeKeyAndOrderFront_(None)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            return str(self.nameText.stringValue())
        return None

    def runModalForRename_(self, name):
        self.nameText.setStringValue_(name)
        self.window.setTitle_(NSLocalizedString("Rename Group", "Window title"))
        self.caption.setStringValue_(NSLocalizedString("Enter a new name for the group:", "Label"))
        self.window.makeKeyAndOrderFront_(None)
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

